"""macrolib and the macro_lib gate (issue #109). The fast half is the spec, the
ceilings, the period rule and the wiring. The slow half is real ngspice and
OpenSTA: an inverter macro against gate caps and a hand bench, a W/4 driver in
top STA, a typed period refused, a running oscillator.

The STA tests run plain OpenSTA on a hand-written top netlist: macro plus two
library flops. No harden runs here, so no routed net, no SPEF and no LibreLane
reading the `lib` view; what the flow does with it has not been run."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "scripts"))
sys.path.insert(0, str(REPO / "engine" / "lib"))

import charlib  # noqa: E402
import check_macro_lib  # noqa: E402
import check_top_harden  # noqa: E402
import macrolib  # noqa: E402
import ttlib  # noqa: E402
from checklib import CheckError  # noqa: E402

EDA = REPO / "bin" / "eda"
TT = "tt_025C_3v30"
SPEC = {"pins": {"a": {"max_transition_ns": 2.0},
                 "y": {"max_transition_ns": 0.5, "max_capacitance_pf": 0.2}},
        "arcs": [{"from": "a", "to": "y", "sense": "negative_unate"}],
        "slews_ns": [0.1, 0.5], "loads_pf": [0.005, 0.05, 0.2]}
PINS = {"a": "d2a", "y": "a2d"}


def _spec(**over):
    return {**json.loads(json.dumps(SPEC)), **over}


def _refused(section, pins=PINS) -> str:
    with pytest.raises(macrolib.Refused) as e:
        macrolib.parse_spec(section, "invm", pins)
    assert e.value.kind == "macro_timing_incomplete"
    return str(e.value)


# ---- the spec


def test_every_limit_and_output_arc_is_the_specs_to_state():
    bad = _spec()
    del bad["pins"]["a"]
    assert "no limits for crossing pin a" in _refused(bad)
    bad = _spec()
    del bad["pins"]["y"]["max_capacitance_pf"]
    assert "max_capacitance_pf" in _refused(bad)
    bad = _spec()
    del bad["pins"]["y"]["max_transition_ns"]
    assert "max_transition_ns" in _refused(bad)
    assert "no arc to output" in _refused(_spec(arcs=[]))
    assert "not crossing pins" in _refused(
        _spec(pins={**SPEC["pins"], "zz": {"max_transition_ns": 1}}))
    assert "give sense" in _refused(_spec(arcs=[{"from": "a", "to": "y"}]))


def test_a_source_arc_states_its_enable_and_window():
    arc = {"to": "y", "source": True, "enable": "a"}
    assert "settle_ns" in _refused(_spec(arcs=[arc]))
    assert "enable" in _refused(_spec(arcs=[{"to": "y", "source": True,
                                             "settle_ns": 1, "window_ns": 2}]))
    ok = macrolib.parse_spec(
        _spec(arcs=[{**arc, "settle_ns": 1, "window_ns": 2}]), "invm", PINS)
    assert ok.arcs[0].source and ok.arcs[0].frm == "a"


def test_a_bus_takes_its_limits_from_the_signal_name():
    mt = macrolib.parse_spec(
        {"pins": {"code": {"max_transition_ns": 1}}}, "m",
        {"code[1]": "d2a", "code[0]": "d2a"})
    assert mt.inputs == ["code[0]", "code[1]"]
    assert mt.limits["code[1]"]["max_transition_ns"] == 1


def test_default_loads_reach_the_spec_limit():
    mt = macrolib.parse_spec({**_spec(), "loads_pf": None}, "invm", PINS)
    assert len(mt.loads) == macrolib.DEFAULT_LOADS
    assert mt.loads[-1] == pytest.approx(0.2)


def test_corner_is_read_from_the_pvt_name():
    c = macrolib.corner_of("ff_n40C_3v60")
    assert (c["process"], c["temp_c"], c["vdd"]) == ("ff", -40, 3.6)
    with pytest.raises(macrolib.Refused):
        macrolib.corner_of("tt_nope")


def test_an_undriven_input_is_refused_not_floated(tmp_path):
    mt = macrolib.parse_spec(_spec(), "invm", PINS)
    bench = macrolib.Bench("invm", tmp_path / "x", ["a", "y", "bias", "vdd", "vss"],
                           "vdd", "vss", tmp_path, macrolib.corner_of(TT), 3.3,
                           tmp_path, None)
    with pytest.raises(macrolib.Refused, match="no level for pin bias"):
        bench.statics(mt, {}, {"a"})
    assert bench.statics(mt, {"bias": "vdd"}, {"a"}) == {"bias": 3.3}


# ---- ceilings and the period rule


def _record(tmp_path, measured, status="pass"):
    path = tmp_path / "rec.json"
    path.write_text(json.dumps({"status": status, "facts": {"measured": measured}}))
    return path


def _clock_mt():
    return macrolib.parse_spec(_spec(clocks={"y": {
        "record": "rec.json", "measure": "f", "divide": 2}}), "invm", PINS)


MEASURED = {"tt_27c": {"f": 250.0}, "tt_pff": {"f": 288.379},
            "ss_125c": {"f": 200.0}, "ss_pff": {"f": 260.664},
            "ff_m40c": {"f": 296.867}, "ff_pss": {"f": 100.0}}


def test_the_ceiling_is_the_fastest_of_the_process_resistor_skews_included(tmp_path):
    _record(tmp_path, MEASURED)
    per = macrolib.clock_ceilings(_clock_mt(), tmp_path)["y"]["processes"]
    assert per["tt"]["corner"] == "tt_pff"
    assert per["ss"]["corner"] == "ss_pff"
    assert per["ss"]["period_ns"] == pytest.approx(2000 / 260.664, abs=1e-3)
    assert per["ff"]["fmax_mhz"] == pytest.approx(296.867 / 2, abs=1e-3)


def test_a_failed_record_or_missing_process_gives_no_ceiling(tmp_path):
    with pytest.raises(macrolib.Refused, match="no record"):
        macrolib.clock_ceilings(_clock_mt(), tmp_path)
    _record(tmp_path, MEASURED, status="fail")
    with pytest.raises(macrolib.Refused, match="not pass"):
        macrolib.clock_ceilings(_clock_mt(), tmp_path)
    _record(tmp_path, {"tt_27c": {"f": 250.0}})
    with pytest.raises(macrolib.Refused, match="process"):
        macrolib.clock_ceilings(_clock_mt(), tmp_path)


def test_a_gate_record_shape_is_read_too(tmp_path):
    path = tmp_path / "rec.json"
    path.write_text(json.dumps({"status": "pass", "facts": {"results": {
        k: {"bench.cir": {"measures": v}} for k, v in MEASURED.items()}}}))
    per = macrolib.clock_ceilings(_clock_mt(), tmp_path)["y"]["processes"]
    assert per["ss"]["recorded_mhz"] == pytest.approx(260.664)


def _ceil(period=6.0, margin=0.05):
    return {"y": {"margin": margin, "processes": {
        p: {"period_ns": period, "fmax_mhz": 1000 / period, "corner": p + "_x"}
        for p in ("tt", "ss", "ff")}}}


@pytest.mark.parametrize("period,kind", [
    (6.0, None), (5.8, None), (6.01, None),  # on it, under by 3.3%, rounding
    (6.2, "macro_clock_disagrees"),          # slower than the macro can run
    (5.4, "macro_clock_disagrees"),          # stale: 10% under
])
def test_period_rule(period, kind):
    found = macrolib.judge_periods(_ceil(), {TT: {"y": period}}, "u_analog")
    assert [f["kind"] for f in found] == ([kind] if kind else [])


def test_period_margin_is_the_specs_to_widen():
    assert not macrolib.judge_periods(_ceil(margin=0.15), {TT: {"y": 5.4}}, "u")


def test_a_missing_or_undeclared_macro_clock_is_a_finding():
    assert macrolib.judge_periods(_ceil(), {TT: {}}, "u")[0]["kind"] == (
        "macro_clock_undefined")
    found = macrolib.judge_periods(_ceil(), {TT: {"y": 6.0, "q": 3.0}}, "u")
    assert [f["kind"] for f in found] == ["macro_clock_undeclared"]


def test_an_sdc_clock_on_the_macro_with_no_declaration_is_refused(tmp_path):
    macro = {"instance": "u_analog", "cell": "m"}
    sdc = "create_clock -name v -period 5.4 [get_pins {u_analog/vco_div2}]\n"
    found = check_top_harden.macro_clock_problems(tmp_path, None, macro, sdc,
                                                  tmp_path, {})
    assert [f["kind"] for f in found] == ["macro_clock_undeclared"]
    # a clock elsewhere, or no SDC at all, is no concern of this check
    assert not check_top_harden.macro_clock_problems(
        tmp_path, None, macro, "create_clock -period 5 [get_ports clk2]\n",
        tmp_path, {})
    assert not check_top_harden.macro_clock_problems(tmp_path, None, macro, None,
                                                     tmp_path, {})


def test_a_declared_clock_with_no_sdc_is_a_finding(tmp_path):
    found = check_top_harden.macro_clock_problems(
        tmp_path, {"ceilings": _ceil()}, {"instance": "u", "cell": "m"}, None,
        tmp_path, {})
    assert [f["kind"] for f in found] == ["macro_clock_undefined"]


# ---- delivery


def test_delivery_refuses_with_no_gate_output_and_copies_every_corner(tmp_path):
    with pytest.raises(macrolib.Refused, match="run the macro_lib gate"):
        macrolib.deliver(tmp_path, "m", tmp_path / "macros")
    src = tmp_path / macrolib.OUT_DIR
    src.mkdir()
    for n in macrolib.STA_CORNERS[:-1]:
        (src / f"m__{n}.lib").write_text("x")
    with pytest.raises(macrolib.Refused, match=macrolib.STA_CORNERS[-1]):
        macrolib.deliver(tmp_path, "m", tmp_path / "macros")
    (src / f"m__{macrolib.STA_CORNERS[-1]}.lib").write_text("x")
    libs = macrolib.deliver(tmp_path, "m", tmp_path / "macros")
    assert set(libs) == set(macrolib.STA_CORNERS)
    assert all(Path(p).is_file() for p in libs.values())


def test_a_lib_from_another_extraction_is_stale(tmp_path):
    src = tmp_path / macrolib.OUT_DIR
    src.mkdir()
    ex = tmp_path / "x.spice"
    ex.write_text("one")
    import hashlib
    (src / "m.report.json").write_text(json.dumps(
        {"extracted_sha256": hashlib.sha256(b"one").hexdigest()}))
    macrolib.check_extracted(tmp_path, "m", ex)
    ex.write_text("two")
    with pytest.raises(macrolib.Refused, match="re-run the macro_lib gate"):
        macrolib.check_extracted(tmp_path, "m", ex)


def _macro(**files):
    return {"cell": "m", "instance": "u", "location": [0, 0],
            "power": {"vdd": "vdd", "vss": "vss"},
            "files": {"gds": "g", "lef": "l", "vh": "v", "spice": "s", **files}}


def test_librelane_gets_the_libs_per_corner_as_the_macros_lib_view():
    conf = ttlib.macro_config([_macro(**{f"lib_{TT}": "/a.lib"})])
    assert conf["MACROS"]["m"]["lib"] == {f"*_{TT}": ["/a.lib"]}
    assert "EXTRA_LIBS" not in conf
    assert "lib" not in ttlib.macro_config([_macro()])["MACROS"]["m"]


def test_the_gate_is_registered_and_owed_only_when_declared(tmp_path):
    imap = yaml.safe_load((REPO / "engine/reference/invalidation.yaml").read_text())
    assert "macro_lib" in imap["gate_inputs"]["msde"]
    assert "macro_lib" in imap["gate_inputs"]["msde"]["top_harden"]
    (tmp_path / "analog" / "spec").mkdir(parents=True)
    spec = tmp_path / "analog" / "spec" / "spec.yaml"
    spec.write_text("top: m\n")
    assert "not applicable" in check_macro_lib.not_applicable_at_release(tmp_path)
    spec.write_text(yaml.safe_dump({"top": "m", "macro_timing": _spec()}))
    assert check_macro_lib.not_applicable_at_release(tmp_path) is None
    spec.write_text("top: [unclosed\n")
    assert check_macro_lib.not_applicable_at_release(tmp_path) is None  # owed


def test_liberty_text_round_trips_with_limits_and_tables():
    mt = macrolib.parse_spec(_spec(), "invm", PINS)
    arc = mt.arcs[0]
    grid = [[0.1, 0.2, 0.3], [0.2, 0.3, 0.4]]
    tabs = {k: grid for k in ("cell_rise", "cell_fall", "rise_transition",
                              "fall_transition")}
    text = macrolib.liberty_text(
        mt, "library (x) {\n  time_unit : 1ns;\n}\n", "note",
        {"a": {"capacitance": 0.02, "rise_capacitance": 0.02,
               "fall_capacitance": 0.019}},
        {"y": [(arc, tabs)]}, None)
    lib = charlib.parse_liberty(re.sub(r"/\*.*?\*/", "", text, flags=re.S))
    cell = lib.sub("cell")[0]
    pins = {g.args.strip('"'): g for g in cell.sub("pin")}
    assert float(pins["a"].attr("max_transition")) == 2.0
    assert float(pins["y"].attr("max_capacitance")) == 0.2
    timing = pins["y"].sub("timing")[0]
    assert timing.attr("timing_sense") == "negative_unate"
    assert charlib.table(timing.sub("cell_rise")[0])[1][2] == pytest.approx(0.4)


def test_limit_warning_when_the_limits_disagree():
    mt = macrolib.parse_spec(_spec(), "invm", PINS)
    tabs = {"rise_transition": [[0.1, 0.3, 0.9], [0.1, 0.3, 0.9]]}
    found = macrolib.limit_warnings(mt, TT, {"y": [(mt.arcs[0], tabs)]})
    assert found[0]["transition_ns"] == pytest.approx(0.9)
    tabs = {"rise_transition": [[0.1, 0.2, 0.4], [0.1, 0.2, 0.4]]}
    assert not macrolib.limit_warnings(mt, TT, {"y": [(mt.arcs[0], tabs)]})


# ---- real ngspice and OpenSTA


def _inverter(wn: float, wp: float) -> str:
    return (".subckt invm a y vdd vss\n"
            f"XP y a vdd vdd pfet_03v3 w={wp}u l=0.28u\n"
            f"XN y a vss vss nfet_03v3 w={wn}u l=0.28u\n.ends\n")


def _char(tmp_path, netlist: str, tag: str, section=None, pins=PINS,
          ports=("a", "y", "vdd", "vss"), cell="invm", corners=(TT,)):
    import layoutlib
    path = tmp_path / f"{tag}.sp"
    path.write_text(netlist)
    mt = macrolib.parse_spec(section or _spec(), cell, pins)
    out = tmp_path / tag
    report = macrolib.characterize(
        mt, netlist=path, netlist_kind="schematic", ports=list(ports), power="vdd",
        ground="vss", pdk=layoutlib.pdk_root(), out_dir=out, corners=list(corners))
    return mt, report, out


def _stdcell(corner=TT) -> Path:
    import layoutlib
    return (layoutlib.pdk_root() / "libs.ref" / macrolib.STA_LIBRARY / "lib"
            / f"{macrolib.STA_LIBRARY}__{corner}.lib")


def _sta(tmp_path, body: str) -> str:
    script = tmp_path / "sta.tcl"
    script.write_text(body)
    run = subprocess.run([str(EDA), "sta", "-no_init", "-exit", str(script)],
                         capture_output=True, text=True, stdin=subprocess.DEVNULL,
                         timeout=300)
    assert run.returncode == 0, run.stdout + run.stderr
    return run.stdout + run.stderr


FLOP = f"{macrolib.STA_LIBRARY}__dffrnq_1"
TOP = f"""module top(input a, input d, input rst_n, output q, output q2);
 wire c;
 invm u_m(.a(a), .y(c));
 {FLOP} f1(.D(d), .CLK(c), .RN(rst_n), .Q(q));
 {FLOP} f2(.D(q), .CLK(c), .RN(rst_n), .Q(q2));
endmodule
"""


def _top_sta(tmp_path, lib: Path, *, period=5.0, net_pf=0.15, driven_slew=None) -> str:
    v = tmp_path / "top.v"
    v.write_text(TOP)
    return _sta(tmp_path, f"""read_liberty {_stdcell()}
read_liberty {lib}
read_verilog {v}
link_design top
create_clock -name v -period {period} [get_pins u_m/y]
set_propagated_clock [all_clocks]
set_load {net_pf} [get_nets c]
{f'set_input_transition {driven_slew} [get_ports a]' if driven_slew else ''}
puts "== slew"
report_check_types -max_slew -violators
puts "== cap"
report_check_types -max_capacitance -violators
puts "== setup"
report_checks -path_delay max -format full_clock_expanded -fields slew
""")


def _direct_transition(tmp_path, netlist: str, slew: float, load: float) -> float:
    """A bench sharing no code with macrolib: a falling input, the output's
    30-70 rise, over gf180's 0.5 derate."""
    import layoutlib
    m = layoutlib.pdk_root() / "libs.tech" / "ngspice"
    ramp = slew * 0.5 / 0.4
    deck = tmp_path / "hand.cir"
    deck.write_text(f"""* hand
.lib {m}/sm141064.ngspice typical
.lib {m}/sm141064.ngspice res_typical
.lib {m}/sm141064.ngspice mimcap_typical
.include {m}/design.ngspice
{netlist}
.temp 25
Vdd vdd 0 3.3
Vss vss 0 0
Va a 0 PWL(0 3.3 5n 3.3 {5 + ramp}n 0 40n 0)
Xd a y vdd vss invm
Cl y 0 {load}p
.tran 1p 40n
.meas tran ta WHEN v(y)=0.99 RISE=1
.meas tran tb WHEN v(y)=2.31 RISE=1
.end
""")
    run = subprocess.run([str(EDA), "ngspice", "-b", str(deck)], capture_output=True,
                         text=True, timeout=300, cwd=tmp_path)
    vals = {k: float(v) for k, v in re.findall(
        r"^\s*(t[ab])\s*=\s*([-\d.e+]+)", run.stdout, re.M)}
    return (vals["tb"] - vals["ta"]) * 1e9 / 0.5


# W*L*Cox + 2*W*Cov, with the model file's typical numbers at L = 0.28 um:
# tox 8.0 nm nfet, 7.9 nm pfet; cgso = cgdo 0.100 fF/um nfet, 0.124 pfet.
def _gate_caps_pf(wn, wp, l=0.28):
    cox_n = 3.9 * 8.854e-12 / 8.0e-9 * 1e3   # fF/um^2
    cox_p = 3.9 * 8.854e-12 / 7.9e-9 * 1e3
    fF = wn * l * cox_n + wp * l * cox_p + 2 * (wn * 0.100 + wp * 0.124)
    return fF * 1e-3


@pytest.mark.slow
def test_inverter_macro_known_answers(tmp_path):
    net = _inverter(4, 8)
    mt, report, out = _char(tmp_path, net, "stock")
    cap = report["corners"][TT]["input_cap_pf"]["a"]["capacitance"]
    want = _gate_caps_pf(4, 8)
    assert cap == pytest.approx(want, rel=0.10), (cap, want)

    text = (out / f"invm__{TT}.lib").read_text()
    lib = charlib.parse_liberty(re.sub(r"/\*.*?\*/", "", text, flags=re.S))
    pin = [g for g in lib.sub("cell")[0].sub("pin") if g.args.strip('"') == "y"][0]
    rise = charlib.table(pin.sub("timing")[0].sub("rise_transition")[0])
    direct = _direct_transition(tmp_path, net, 0.1, 0.05)
    assert rise[0][1] == pytest.approx(direct, rel=0.10), (rise, direct)
    assert rise[0][0] < rise[0][1] < rise[0][2]  # heavier load, slower edge
    assert not report["warnings"]


@pytest.mark.slow
def test_weak_driver_violates_in_top_sta_and_the_stock_driver_does_not(tmp_path):
    _mt, _r, stock = _char(tmp_path, _inverter(4, 8), "stock")
    _mt, weak_report, weak = _char(tmp_path, _inverter(1, 2), "weak")
    assert weak_report["warnings"]  # its own limits no longer agree either
    good = _top_sta(tmp_path, stock / f"invm__{TT}.lib")
    assert "max slew" not in good.lower().split("== setup")[0].split("== slew")[1]
    assert "MET" in good
    bad = _top_sta(tmp_path, weak / f"invm__{TT}.lib")
    section = bad.split("== slew")[1].split("== cap")[0]
    assert "u_m/y" in section and "VIOLATED" in section, section


@pytest.mark.slow
def test_a_period_disagreeing_with_the_record_is_refused_through_real_sta(tmp_path):
    _mt, _r, lib_dir = _char(tmp_path, _inverter(4, 8), "stock")
    for n in macrolib.STA_CORNERS:
        if n != TT:
            (lib_dir / f"invm__{n}.lib").write_text(
                f"library (invm__{n}) {{ delay_model : table_lookup; }}\n")
    ceilings = _ceil(6.0)
    table = macrolib.period_table_tcl(ceilings)
    work = tmp_path / "w"

    def problems(text):
        sdc = tmp_path / "c.sdc"
        sdc.write_text(text)
        return macrolib.check_sdc(sdc, lib_dir, "invm", ceilings, "u_analog", work)

    # the generated proc gives the record's number at every corner
    assert not problems(
        table + "create_clock -name v -period [macro_clock_period y] "
                "[get_pins {u_analog/y}]\n")
    # a typed literal that agrees passes
    assert not problems("create_clock -period 6.0 -name v [get_pins u_analog/y]\n")
    # one that doesn't, at each corner
    found = problems("create_clock -period 7.5 -name v [get_pins u_analog/y]\n")
    assert {f["corner"] for f in found} == set(macrolib.STA_CORNERS)
    assert {f["kind"] for f in found} == {"macro_clock_disagrees"}
    # a table picked by the loaded liberty, wrong at one process only: PLL's shape
    found = problems(
        "set t {ss 6.0 tt 5.4 ff 6.0}\nproc per {} {global t\n"
        " foreach l [get_libs -quiet *] {set n [get_name $l]\n"
        "  dict for {c p} $t {if {[string match \"*__${c}_*\" $n]} {return $p}}}\n"
        " error nolib}\n"
        "create_clock -name v -period [per] [get_pins {u_analog/y}]\n"
        "set_false_path -from [get_clocks v]\n")
    assert [f["corner"] for f in found] == [TT]
    # no clock on it at all
    found = problems("create_clock -period 5 -name v [get_ports clk]\n")
    assert {f["kind"] for f in found} == {"macro_clock_undefined"}
    # an SDC that cannot be evaluated is refused, never a pass
    sdc = tmp_path / "c.sdc"
    sdc.write_text("error boom\n")
    with pytest.raises(macrolib.Refused, match="boom"):
        macrolib.sdc_clocks(sdc, lib_dir / f"invm__{TT}.lib", work)


RING = """.subckt ring en y vdd vss
XP1 n1 en vdd vdd pfet_03v3 w=2u l=0.28u
XP2 n1 y vdd vdd pfet_03v3 w=2u l=0.28u
XN1 n1 en m vss nfet_03v3 w=2u l=0.28u
XN2 m y vss vss nfet_03v3 w=2u l=0.28u
XP3 n2 n1 vdd vdd pfet_03v3 w=2u l=0.28u
XN3 n2 n1 vss vss nfet_03v3 w=1u l=0.28u
XP4 y n2 vdd vdd pfet_03v3 w=2u l=0.28u
XN4 y n2 vss vss nfet_03v3 w=1u l=0.28u
.ends
"""


@pytest.mark.slow
def test_a_running_oscillator_is_read_from_its_own_waveform(tmp_path):
    section = {"pins": {"en": {"max_transition_ns": 2.0},
                        "y": {"max_transition_ns": 1.0, "max_capacitance_pf": 0.03}},
               "static": {"en": "vdd"},
               "arcs": [{"to": "y", "source": True, "enable": "en",
                         "settle_ns": 10, "window_ns": 60}],
               # heavier than this y is the ring's one dominant pole and it stops,
               # which the bench refuses: a real 0.1 pF run did
               "loads_pf": [0.002, 0.01, 0.03]}
    _mt, report, out = _char(tmp_path, RING, "ring", section,
                             {"en": "d2a", "y": "a2d"},
                             ("en", "y", "vdd", "vss"), "ring")
    f = report["corners"][TT]["running_mhz_by_load"]["y"]
    assert f[0] > f[1] > f[2] > 0  # a heavier load slows the ring through y
    lib = out / f"ring__{TT}.lib"
    text = lib.read_text()
    assert "cell_rise" not in text  # no delay was measured, so none is written
    top = tmp_path / "t.v"
    top.write_text(f"""module top(input en, input d, input rst_n, output q, output q2);
 wire c;
 ring u_m(.en(en), .y(c));
 {FLOP} f1(.D(d), .CLK(c), .RN(rst_n), .Q(q));
 {FLOP} f2(.D(q), .CLK(c), .RN(rst_n), .Q(q2));
endmodule
""")
    out_sta = _sta(tmp_path, f"""read_liberty {_stdcell()}
read_liberty {lib}
read_verilog {top}
link_design top
create_clock -name v -period 10 [get_pins u_m/y]
set_propagated_clock [all_clocks]
set_load 0.01 [get_nets c]
report_checks -to f2/D -format full_clock_expanded -fields slew
""")
    # the pin's slew in STA is the table's at that load
    rise = charlib.table(next(
        g for g in charlib.parse_liberty(re.sub(r"/\*.*?\*/", "", text, flags=re.S))
        .sub("cell")[0].sub("pin") if g.args.strip('"') == "y")
        .sub("timing")[0].sub("rise_transition")[0])
    assert 0 < rise[0][0] < rise[0][1] < rise[0][2]  # a transition is never negative
    slew = re.search(r"^\s+(\d+\.\d+)\s+0\.00\s+\S+ \^ u_m/y", out_sta, re.M)
    assert slew, out_sta
    # set_load 0.01 plus two flops' CLK pins: between the 0.01 and 0.03 columns
    assert rise[0][1] < float(slew.group(1)) < rise[0][2], out_sta


@pytest.mark.slow
def test_a_bus_macro_links_in_sta(tmp_path):
    net = (".subckt busm code[1] code[0] y vdd vss\n"
           "XP y code[0] vdd vdd pfet_03v3 w=2u l=0.28u\n"
           "XN y code[0] vss vss nfet_03v3 w=1u l=0.28u\n"
           "XP1 q code[1] vdd vdd pfet_03v3 w=2u l=0.28u\n"
           "XN1 q code[1] vss vss nfet_03v3 w=1u l=0.28u\n.ends\n")
    section = {"pins": {"code": {"max_transition_ns": 2.0},
                        "y": {"max_transition_ns": 2.0, "max_capacitance_pf": 0.1}},
               "static": {"code[1]": 0, "code[0]": 0},
               "arcs": [{"from": "code[0]", "to": "y", "sense": "negative_unate"}],
               "slews_ns": [0.1], "loads_pf": [0.01, 0.1]}
    _mt, report, out = _char(
        tmp_path, net, "bus", section,
        {"code[1]": "d2a", "code[0]": "d2a", "y": "a2d"},
        ("code[1]", "code[0]", "y", "vdd", "vss"), "busm")
    caps = report["corners"][TT]["input_cap_pf"]
    assert caps["code[0]"]["capacitance"] > 0.003
    top = tmp_path / "t.v"
    top.write_text("module top(input [1:0] c, output o);\n"
                   " busm u(.code(c), .y(o));\nendmodule\n")
    got = _sta(tmp_path, f"""read_liberty {out}/busm__{TT}.lib
read_verilog {top}
link_design top
report_net c[0]
report_checks -from c[0] -unconstrained
""")
    assert "not found" not in got.lower(), got
    assert "u/code[0]" in got, got
    assert "No paths found" not in got, got  # the arc from the bus bit is timed


def _ws(tmp_path, section, netlist=None) -> Path:
    ws = tmp_path / "ws"
    (ws / "analog" / "spec").mkdir(parents=True)
    (ws / "analog" / "netlist").mkdir()
    (ws / "analog" / "state.json").write_text(json.dumps({"block": "invm"}))
    (ws / "digital" / "rtl").mkdir(parents=True)
    (ws / "analog" / "netlist" / "invm.cir").write_text(netlist or _inverter(4, 8))
    (ws / "analog" / "spec" / "spec.yaml").write_text(yaml.safe_dump(
        {"top": "invm", **({"macro_timing": section} if section else {})}))
    (ws / "interface.yaml").write_text(yaml.safe_dump({"version": 1, "signals": [
        {"name": "a", "direction": "d2a", "level": "cmos_3v3", "width": 1},
        {"name": "y", "direction": "a2d", "level": "cmos_3v3", "width": 1}]}))
    return ws


def test_gate_without_a_declaration_warns_and_passes(tmp_path):
    payload, _ = check_macro_lib.run(["--workspace", str(_ws(tmp_path, None))])
    # warning only: the gate's fail_severities are [error], so gate.py passes it
    assert payload["violations"][0]["kind"] == "macro_timing_undeclared"
    assert payload["violations"][0]["severity"] == "warning"


@pytest.mark.slow
def test_gate_writes_a_lib_per_corner_slow_at_ss_and_fast_at_ff(tmp_path):
    section = {**_spec(), "slews_ns": [0.1], "loads_pf": [0.01, 0.1]}
    ws = _ws(tmp_path, section)
    payload, _ = check_macro_lib.run(["--workspace", str(ws)])
    assert not [v for v in payload["violations"] if v["severity"] == "error"], payload
    assert payload["netlist_kind"] == "schematic"
    tables = {}
    for n in macrolib.STA_CORNERS:
        text = (ws / macrolib.OUT_DIR / f"invm__{n}.lib").read_text()
        assert str(ws) not in text
        lib = charlib.parse_liberty(re.sub(r"/\*.*?\*/", "", text, flags=re.S))
        pin = [g for g in lib.sub("cell")[0].sub("pin") if g.args.strip('"') == "y"][0]
        tables[n] = charlib.table(pin.sub("timing")[0].sub("cell_rise")[0])[0][1]
    assert tables["ss_125C_3v00"] > tables["tt_025C_3v30"] > tables["ff_n40C_3v60"]
    # nothing machine-specific or scratch in what top_harden hashes and copies
    assert sorted(p.suffix for p in (ws / macrolib.OUT_DIR).iterdir()) == [
        ".json", ".json", ".lib", ".lib", ".lib"]
    assert str(tmp_path) not in (ws / macrolib.OUT_DIR / "invm.report.json").read_text()


def test_gate_refuses_an_incomplete_declaration_before_any_sim(tmp_path):
    bad = _spec()
    del bad["pins"]["y"]
    ws = _ws(tmp_path, bad)
    with pytest.raises(macrolib.Refused, match="no limits for crossing pin y"):
        check_macro_lib.run(["--workspace", str(ws)])
    assert not (ws / macrolib.OUT_DIR).exists()


def test_top_harden_says_what_to_run_when_the_gate_has_not(tmp_path):
    ws = _ws(tmp_path, _spec())
    macro = {"cell": "invm", "instance": "u_analog", "files": {}}
    with pytest.raises(CheckError, match="run the macro_lib gate"):
        check_top_harden.macro_abstract(ws, ws / "analog", macro, tmp_path / "top")


def _timing(**extra):
    return macrolib.parse_spec(
        {"pins": {"a": {"max_transition_ns": 1.0},
                  "y": {"max_transition_ns": 1.0, "max_capacitance_pf": 0.1}},
         "arcs": [{"from": "a", "to": "y", "sense": "negative_unate"}], **extra},
        "invm", {"a": "d2a", "y": "a2d"})


def test_a_named_include_is_read_by_each_deck(tmp_path, monkeypatch):
    (tmp_path / "pdk").mkdir()
    monkeypatch.setattr(macrolib.charlib, "model_files",
                        lambda pdk: ("models", "design"))
    monkeypatch.setattr(macrolib.simlib, "run_ngspice",
                        lambda *a, **k: ("", "", 0))
    bench = macrolib.Bench("invm", tmp_path / "x", ["a", "y", "vdd", "vss"],
                           "vdd", "vss", tmp_path / "pdk",
                           macrolib.corner_of("tt_025C_3v30"), 3.3, tmp_path,
                           None, includes=[tmp_path / "moscap.spice"])
    bench.simulate({}, {}, 1, 0.1, [])
    deck = (tmp_path / "bench_1.cir").read_text()
    assert f".include {tmp_path / 'moscap.spice'}" in deck
    assert deck.index("moscap.spice") < deck.index(f".include {tmp_path / 'x'}")


def test_includes_missing_refused_and_a_changed_one_is_stale(tmp_path):
    mt = _timing(includes=["spec/moscap.spice"])
    mt.cell = "invm"
    analog = tmp_path / "analog"
    with pytest.raises(macrolib.Refused, match="no file"):
        macrolib.resolve_includes(mt, analog)
    (analog / "spec").mkdir(parents=True)
    inc = analog / "spec" / "moscap.spice"
    inc.write_text("* one\n")
    out = tmp_path / macrolib.OUT_DIR
    out.mkdir(parents=True)
    (out / "invm.report.json").write_text(json.dumps(
        {"includes_sha256": macrolib.includes_digest([inc])}))
    macrolib.check_includes(tmp_path, mt, analog)
    inc.write_text("* two\n")
    with pytest.raises(macrolib.Refused, match="includes"):
        macrolib.check_includes(tmp_path, mt, analog)


def test_no_includes_reads_clean_against_an_old_report(tmp_path):
    mt = _timing()
    mt.cell = "invm"
    out = tmp_path / macrolib.OUT_DIR
    out.mkdir(parents=True)
    (out / "invm.report.json").write_text("{}")
    macrolib.check_includes(tmp_path, mt, tmp_path / "analog")


def test_a_pad_cap_pin_stays_undriven_and_carries_the_cap(tmp_path, monkeypatch):
    (tmp_path / "pdk").mkdir()
    monkeypatch.setattr(macrolib.charlib, "model_files",
                        lambda pdk: ("models", "design"))
    monkeypatch.setattr(macrolib.simlib, "run_ngspice",
                        lambda *a, **k: ("", "", 0))
    mt = _timing(pad_caps_pf={"bias": 5}, static={})
    bench = macrolib.Bench("invm", tmp_path / "x", ["a", "y", "bias", "vdd", "vss"],
                           "vdd", "vss", tmp_path / "pdk",
                           macrolib.corner_of("tt_025C_3v30"), 3.3, tmp_path,
                           None, pad_caps=mt.pad_caps)
    assert "bias" not in bench.statics(mt, {}, {"a"})
    bench.simulate({}, {}, 1, 0.1, [])
    deck = (tmp_path / "bench_1.cir").read_text()
    assert "Cpad_n2 n2 0 5p" in deck
    assert "Vn2 " not in deck


def test_a_pin_with_neither_level_nor_pad_cap_is_still_refused(tmp_path):
    mt = _timing()
    bench = macrolib.Bench("invm", tmp_path / "x", ["a", "y", "bias", "vdd", "vss"],
                           "vdd", "vss", tmp_path, macrolib.corner_of("tt_025C_3v30"),
                           3.3, tmp_path, None)
    with pytest.raises(macrolib.Refused, match="no level for pin bias"):
        bench.statics(mt, {}, {"a"})


def test_macro_pins_dropped_names_a_port_the_netlist_lost():
    import check_top_harden as cth
    nl = """ gf180mcu_x u1 (.I(a));
 pll_analog u_analog (.vco_div2(\\u.vco_div2 ),
    .pfd_up(net56),
    .cp_trim0(),
    .vctrl(ua[0]));
 gf180mcu_y u2 (.bias_ref(z));
"""
    want = {"vco_div2", "pfd_up", "cp_trim0", "vctrl", "bias_ref"}
    assert cth.macro_pins_dropped(nl, "pll_analog", "u_analog", want) == [
        "bias_ref", "cp_trim0"]
    assert cth.macro_pins_dropped(nl, "pll_analog", "other", {"a"}) == ["a"]


def test_a_pad_pin_is_named_in_the_liberty_with_no_timing():
    mt = macrolib.parse_spec(_spec(), "invm", PINS)
    arc = mt.arcs[0]
    grid = [[0.1, 0.2, 0.3], [0.2, 0.3, 0.4]]
    tabs = {k: grid for k in ("cell_rise", "cell_fall", "rise_transition",
                              "fall_transition")}
    text = macrolib.liberty_text(
        mt, "library (x) {\n  time_unit : 1ns;\n}\n", "note",
        {"a": {"capacitance": 0.02, "rise_capacitance": 0.02,
               "fall_capacitance": 0.019}},
        {"y": [(arc, tabs)]}, None, other_pins=["vctrl"])
    lib = charlib.parse_liberty(re.sub(r"/\*.*?\*/", "", text, flags=re.S))
    pins = {g.args.strip('"'): g for g in lib.sub("cell")[0].sub("pin")}
    assert pins["vctrl"].attr("direction") == "inout"
    assert not pins["vctrl"].sub("timing")

