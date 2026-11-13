"""engine/scripts/check_top_stress.py: msde's top_stress gate (device
overstress on the assembled top's digital standard cells).

Fast tests read a small SPEF, liberty and powered netlist through the
gate's parsers, check the every-state walk and the refusals, and turn a
stubbed failing row into a named finding. The slow test runs ngspice for real
on the PDK's own cells with one planted cell added: an input edge that
bootstraps a gate above the rail, which the gate must name, while the real
inverter it drives stays clean."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import check_lvs  # noqa: E402
import check_top_stress as ts  # noqa: E402
import stresslib  # noqa: E402
import ttlib  # noqa: E402
from checklib import CheckError  # noqa: E402

LIB = "gf180mcu_fd_sc_mcu7t5v0"
BOOT, INV, TIE = f"{LIB}__boot_1", f"{LIB}__inv_1", f"{LIB}__tieh"

# An input edge couples through C1 onto the gate of M1, which R1 holds at
# VDD: each rising edge lifts the gate about one supply above the rail.
BOOT_SPICE = f"""\
.SUBCKT {BOOT} A Z VDD VNW VPW VSS
X_M1 Z G VSS VPW nfet_05v0 W=1e-06 L=6e-07
R1 G VDD 1k
C1 A G 1p
.ENDS
"""
BOOT_LIBERTY = f"""
  cell ({BOOT}) {{
    pg_pin(VDD) {{ pg_type : primary_power ; }}
    pin(A) {{
      direction : input ;
      capacitance : 0.002 ;
      rise_capacitance : 0.003 ;
    }}
    pin(Z) {{
      direction : output ;
      max_capacitance : 0.3 ;
    }}
  }}
"""
FAKE_SPICE = f"""\
.SUBCKT {INV} I ZN VDD VNW VPW VSS
X_M1 ZN I VSS VPW nfet_05v0 W=7.1e-07 L=6e-07
X_M2 ZN I VDD VNW pfet_05v0 W=1.2e-06 L=5e-07
.ENDS
.SUBCKT {TIE} Z VDD VNW VPW VSS
X_M1 Z VSS VDD VNW pfet_05v0 W=1e-06 L=5e-07
.ENDS
.SUBCKT {LIB}__fill_1 VDD VNW VPW VSS
.ENDS
""" + BOOT_SPICE
FAKE_LIBERTY = f"""library (x) {{
  cell ({INV}) {{
    pin(I) {{
      direction : input ;
      capacitance : 0.004 ;
    }}
    pin(ZN) {{
      direction : output ;
      max_capacitance : 0.2 ;
    }}
  }}
  cell ({TIE}) {{
    pin(Z) {{
      direction : output ;
    }}
  }}
{BOOT_LIBERTY}}}
"""
POWERED = f"""\
module tt_um_x (VPWR, VGND, a, y);
  inout VPWR;
  inout VGND;
  input a;
  output y;
  wire n1;
  {BOOT} u_boot (.A(a), .Z(n1), .VDD(VPWR), .VNW(VPWR), .VPW(VGND), .VSS(VGND));
  {INV} u_inv (.I(n1), .ZN(y), .VDD(VPWR), .VNW(VPWR), .VPW(VGND), .VSS(VGND));
  {LIB}__fill_1 f0 (.VDD(VPWR), .VNW(VPWR), .VPW(VGND), .VSS(VGND));
endmodule
"""


def _spef(n1_pf=0.01, y_pf=0.02):
    return f"""\
*SPEF "ieee 1481-1999"
*DESIGN "tt_um_x"
*T_UNIT 1 NS
*C_UNIT 1 PF
*R_UNIT 1 OHM

*NAME_MAP
*1 n1
*2 u_boot
*3 u_inv
*4 y

*PORTS
y O

*D_NET *1 {n1_pf}
*CONN
*I *2:Z O *D {BOOT}
*I *3:I I *D {INV}
*CAP
1 *2:Z 0.001
*END

*D_NET *4 {y_pf}
*CONN
*P *4 O
*I *3:ZN O *D {INV}
*CAP
1 *4 0.002
*END
"""


def _workspace(tmp_path, monkeypatch, pnl=POWERED, vdd=3.3, spec=None,
               library=None):
    """A hardened top's files. `library` replaces the PDK's text (None: the
    real PDK's cells and liberty with the planted cell added)."""
    ws = tmp_path / "blk"
    top = ws / "top"
    (top / "spec").mkdir(parents=True)
    (top / "spec" / "spec.yaml").write_text(yaml.safe_dump(
        spec if spec is not None else
        {"name": "x", "top": "x", "clock": {"period_ns": 20}}))
    final = top / "harden" / "runs" / "run" / "final"
    (final / "pnl").mkdir(parents=True)
    (final / "pnl" / "tt_um_x.pnl.v").write_text(pnl)
    for bucket, scale in (("min", 1.0), ("max", 2.0)):
        d = final / "spef" / bucket
        d.mkdir(parents=True)
        (d / f"tt_um_x.{bucket}.spef").write_text(
            _spef(0.01 * scale, 0.02 * scale))
    (ws / "analog" / "spec").mkdir(parents=True)
    (ws / "analog" / "spec" / "spec.yaml").write_text(
        yaml.safe_dump({"supply": {"vdd": vdd}}))
    if library is not None:
        monkeypatch.setattr(check_lvs, "_pdk_root", lambda: tmp_path / "pdk")
        monkeypatch.setattr(ts, "library", lambda pdk: library)
    else:
        real = ts.library

        def planted(pdk):
            spice, liberty = real(pdk)
            return spice + BOOT_SPICE, liberty + BOOT_LIBERTY
        monkeypatch.setattr(ts, "library", planted)
    return ws


def _run(ws, tmp_path, *extra):
    out = tmp_path / "out.json"
    code = ts.main(["--workspace", str(ws), "--out", str(out), *extra])
    return code, json.loads(out.read_text())


def _refusal(ws, capsys):
    code = ts.main(["--workspace", str(ws)])
    return code, json.loads(capsys.readouterr().out)


# ------------------------------------------------------------------- parsers

def test_spef_reads_each_net_with_its_driver_and_sinks():
    nets = ts.parse_spef(_spef())
    assert [n["cap_pf"] for n in nets] == [0.01, 0.02]
    assert ("I", "u_boot", "Z", "O", BOOT) in nets[0]["conns"]
    assert ("I", "u_inv", "I", "I", INV) in nets[0]["conns"]
    assert ("P", "y", None, "O", None) in nets[1]["conns"]


def test_spef_in_another_unit_is_refused():
    with pytest.raises(CheckError, match="C_UNIT"):
        ts.parse_spef(_spef().replace("*C_UNIT 1 PF", "*C_UNIT 1 FF"))


def test_liberty_reads_direction_and_input_capacitance_only():
    pins = ts.liberty_pins(FAKE_LIBERTY, BOOT)
    assert pins == {"A": {"direction": "input", "capacitance_pf": 0.002},
                    "Z": {"direction": "output", "capacitance_pf": None}}
    with pytest.raises(CheckError, match="no cell"):
        ts.liberty_pins(FAKE_LIBERTY, f"{LIB}__nosuch_1")


def test_load_is_the_net_plus_every_standard_cell_pin_on_it():
    spefs = {"min": ts.parse_spef(_spef()), "max": ts.parse_spef(_spef(0.05))}
    loads = ts.cell_loads(BOOT, ["Z"], 1, spefs, FAKE_LIBERTY, {}, {})
    assert loads["Z"]["min"] == pytest.approx(0.014)
    assert loads["Z"]["max"] == pytest.approx(0.054)
    assert loads["Z"]["ports"] == 0


def test_a_net_leaving_the_top_is_counted_under_ports():
    spefs = {b: ts.parse_spef(_spef()) for b in ("min", "max")}
    loads = ts.cell_loads(INV, ["ZN"], 1, spefs, FAKE_LIBERTY, {}, {})
    assert loads["ZN"] == {"min": 0.02, "max": 0.02, "ports": 1}


MACRO_LIB_TEXT = """library (m) {
  cell (pll_analog) {
    pin ("pll_en") {
      direction : input ;
      capacitance : 0.058 ;
    }
    pin ("trim") {
      direction : input ;
    }
  }
}
"""


def _macro_spef(pin):
    return _spef().replace(f"*I *3:I I *D {INV}",
                           f"*I *3:I I *D {INV}\n*I *5:{pin} I *D pll_analog")


def test_a_macro_pin_on_the_net_loads_it_from_the_macros_own_lib(tmp_path):
    lib = tmp_path / "pll_analog__ff_n40C_3v60.lib"
    lib.write_text(MACRO_LIB_TEXT)
    spefs = {b: ts.parse_spef(_macro_spef("pll_en")) for b in ("min", "max")}
    loads = ts.cell_loads(BOOT, ["Z"], 1, spefs, FAKE_LIBERTY, {},
                          {"pll_analog": lib})
    assert loads["Z"]["max"] == pytest.approx(0.01 + 0.004 + 0.058)


@pytest.mark.parametrize("pin, libfile, match", [
    ("pll_en", None, "no lib_ff_n40C_3v60 lib"),
    ("trim", "ok", "no capacitance for pll_analog pin trim"),
])
def test_a_sink_with_no_capacitance_on_record_is_refused(tmp_path, pin,
                                                         libfile, match):
    lib = tmp_path / "pll_analog__ff_n40C_3v60.lib"
    if libfile:
        lib.write_text(MACRO_LIB_TEXT)
    spefs = {b: ts.parse_spef(_macro_spef(pin)) for b in ("min", "max")}
    with pytest.raises(CheckError, match=match):
        ts.cell_loads(BOOT, ["Z"], 1, spefs, FAKE_LIBERTY, {},
                      {"pll_analog": lib if libfile else None})


def test_an_unknown_standard_cell_sink_pin_is_refused():
    spefs = {b: ts.parse_spef(_spef().replace("*3:I I", "*3:Q I"))
             for b in ("min", "max")}
    with pytest.raises(CheckError, match=f"no capacitance for {INV} pin Q"):
        ts.cell_loads(BOOT, ["Z"], 1, spefs, FAKE_LIBERTY, {}, {})


def test_an_instance_on_no_net_makes_the_light_load_zero():
    spefs = {b: ts.parse_spef(_spef()) for b in ("min", "max")}
    loads = ts.cell_loads(BOOT, ["Z"], 2, spefs, FAKE_LIBERTY, {}, {})
    assert loads["Z"] == {"min": 0.0, "max": pytest.approx(0.014), "ports": 0}


def test_a_pin_the_liberty_does_not_place_is_refused():
    lib = ts.liberty_pins(FAKE_LIBERTY, BOOT)
    assert ts.signal_pins(BOOT, ["A", "Z", "VDD", "VNW", "VPW", "VSS"], lib) \
        == (["A"], ["Z"])
    with pytest.raises(CheckError, match="neither a rail"):
        ts.signal_pins(BOOT, ["A", "Z", "EN", "VDD", "VSS"], lib)


@pytest.mark.parametrize("n", [0, 1, 2, 3])
def test_the_walk_toggles_every_input_away_and_back_from_every_state(n):
    ins = [f"A{i}" for i in range(n)]
    pts, slots = ts.walk(ins, 1.0, 1.0)
    assert slots % 2 == 0 and slots >= 2
    # every edge starts on a whole slot and ramps over EDGE_S
    edges = {p: [(round(a), b, d) for (a, b), (_c, d)
                 in zip(pts[p][1::2], pts[p][2::2])] for p in ins}
    times = sorted(t for p in ins for t, _a, _b in edges[p])
    assert times == list(range(1, len(times) + 1))
    assert slots >= len(times)
    states = set()
    level = {p: 0 for p in ins}
    for t in times:
        p = next(q for q in ins if any(e[0] == t for e in edges[q]))
        before = tuple(level[q] for q in ins)
        level[p] ^= 1
        states.add((before, p))
    # each input edge from each of the 2^n states
    assert len(states) == n * 2 ** n


def test_the_slot_is_half_the_fastest_period_the_spec_names():
    spec = {"clock": {"period_ns": 80},
            "timing_notes": {"fast": {"period_ns": 5.4}, "sdc": {"note": "x"}}}
    assert ts.fastest_period_ns(spec) == 5.4
    with pytest.raises(CheckError, match="period_ns"):
        ts.fastest_period_ns({"clock": {"period_ns": True}})


def test_the_bench_drives_inputs_loads_outputs_and_rails_the_wells():
    text, window, outer = ts.bench(BOOT, "boot.sp", ["A", "Z", "VDD", "VNW",
                                                     "VPW", "VSS"],
                                   ["A"], ["Z"], {"Z": 0.014}, Path("/pdk"),
                                   "ff", 3.63, 10e-9)
    assert outer == ["n_a", "n_z", "vdd", "vdd", "0", "0"]
    assert ".lib '/pdk/libs.tech/ngspice/sm141064.ngspice' ff" in text
    assert "c_Z n_Z 0 0.014p" in text and "vsupply vdd 0 3.63" in text
    assert ".temp -40" in text
    assert window.period == 20e-9 and window.cycles * 2 == 6


# ----------------------------------------------------------------- refusals

def _fake(tmp_path, monkeypatch, **kw):
    return _workspace(tmp_path, monkeypatch,
                      library=(FAKE_SPICE, FAKE_LIBERTY), **kw)


@pytest.mark.parametrize("edit, match", [
    (lambda t: t.replace(f"{INV} u_inv", "inv_1 u_inv"), "neither"),
    (lambda t: t.replace(f"{INV} u_inv", f"{LIB}__nosuch_1 u_inv"),
     "no .subckt"),
])
def test_a_cell_outside_the_library_is_a_refusal(tmp_path, monkeypatch, capsys,
                                                 edit, match):
    ws = _fake(tmp_path, monkeypatch, pnl=edit(POWERED))
    code, payload = _refusal(ws, capsys)
    assert code == 2 and match in payload["error"], payload


def test_a_top_with_no_clock_period_is_a_refusal(tmp_path, monkeypatch, capsys):
    ws = _fake(tmp_path, monkeypatch, spec={"name": "x", "top": "x"})
    code, payload = _refusal(ws, capsys)
    assert code == 2 and "period_ns" in payload["error"], payload


def test_a_missing_spef_is_a_refusal(tmp_path, monkeypatch, capsys):
    ws = _fake(tmp_path, monkeypatch)
    (ws / "top/harden/runs/run/final/spef/max/tt_um_x.max.spef").unlink()
    code, payload = _refusal(ws, capsys)
    assert code == 2 and "max SPEF" in payload["error"], payload


def test_a_cell_holding_an_unrated_subcircuit_is_a_refusal(tmp_path, monkeypatch,
                                                          capsys):
    spice = FAKE_SPICE.replace("X_M1 Z G VSS VPW nfet_05v0",
                               "X_M1 Z G VSS VPW mystery_cell")
    ws = _workspace(tmp_path, monkeypatch, library=(spice, FAKE_LIBERTY))
    monkeypatch.setattr(ts, "run_cell", lambda job: pytest.fail("ran"))
    code, payload = _refusal(ws, capsys)
    assert code == 2 and "mystery_cell" in payload["error"], payload


def test_a_failing_row_is_a_named_finding_with_the_instance_count(
        tmp_path, monkeypatch):
    ws = _fake(tmp_path, monkeypatch)

    def fake_run(job):
        dev = job["devices"][0]
        rows = []
        if job["cell"] == BOOT:
            rows = [{"device": dev["name"], "label": dev["label"],
                     "model": dev["model"], "pair": "Vgs", "class": "gate",
                     "limit_v": 5.5, "status": "fail",
                     "reasons": ["one-off peak 1.6 V over"],
                     "rows": [{"kind": "repeating", "peak_v": 7.1,
                               "share": 0.12}]}]
        return {"rows": rows, "repeat": None,
                "worst": {"device": dev["label"], "pair": "Vgs",
                          "class": "gate", "peak_v": 7.1, "limit_v": 5.5}}
    monkeypatch.setattr(ts, "run_cell", fake_run)
    code, payload = _run(ws, tmp_path)
    assert code == 1, payload
    (v,) = payload["violations"]
    assert v["kind"] == "device_overstress" and v["module"] == BOOT
    assert "1 instance(s)" in v["msg"]
    (o,) = payload["over_limit"]
    assert (o["device"], o["pair"], o["peak_v"], o["share"], o["instances"]) \
        == (f"{BOOT}/X_M1", "Vgs", 7.1, 0.12, 1)
    cells = {c["cell"]: c for c in payload["cells"]}
    assert cells[f"{LIB}__fill_1"]["devices"] == 0
    assert [n["element"] for n in cells[BOOT]["not_scored"]] == ["R1", "C1"]
    assert cells[BOOT]["loads_pf"]["Z"]["max"] == pytest.approx(0.024)
    assert payload["supply_v"] == pytest.approx(3.63)



def test_the_stamped_digest_is_what_state_records_against(tmp_path, monkeypatch):
    """state.record_gate refuses a result whose input_digest is not the hash
    of the gate's first input kind (top_pnl, the pnl directory): stamping the
    .pnl.v file would leave no top_stress result recordable (#329)."""
    import statelib
    ws = _fake(tmp_path, monkeypatch)
    def clean(job):
        dev = job["devices"][0]
        return {"rows": [], "repeat": None,
                "worst": {"device": dev["label"], "pair": "Vgs",
                          "class": "gate", "peak_v": 3.7, "limit_v": 5.5}}
    monkeypatch.setattr(ts, "run_cell", clean)
    code, payload = _run(ws, tmp_path)
    assert code == 0 and payload["status"] == "pass", payload
    imap = statelib.load_map()
    first = imap["gate_inputs"]["msde"]["top_stress"][0]
    assert payload["input_digest"] == statelib.hash_kind(ws, first, imap)[1]

def test_diodes_and_passives_are_listed_not_scored_and_others_refused():
    antenna = (f".SUBCKT {LIB}__antenna I VDD VNW VPW VSS\n"
               "D0 VPW I diode_nd2ps_06v0 AREA=0.2052p PJ=1.86u M=1\n"
               "D1 I VNW diode_pd2nw_06v0 AREA=0.2052p PJ=1.86u M=1\n.ENDS\n")
    got = ts.unscored_elements(antenna, f"{LIB}__antenna")
    assert [(g["element"], g["model"]) for g in got] == [
        ("D0", "diode_nd2ps_06v0"), ("D1", "diode_pd2nw_06v0")]
    assert all("drain-bulk junction" in g["reason"] for g in got)
    assert [g["element"] for g in ts.unscored_elements(BOOT_SPICE, BOOT)] \
        == ["R1", "C1"]
    with pytest.raises(CheckError, match="M1"):
        ts.unscored_elements(BOOT_SPICE.replace("X_M1", "M1"), BOOT)


def _pairs(limits):
    dev = {"name": "X_M1", "label": "c/X_M1", "model": "nfet_05v0",
           "nodes": ["d", "g", "s", "b"],
           "vectors": ["v(d)", "v(g)", "v(s)", "v(b)"]}
    return stresslib.pair_list([dev], limits)


def test_a_peak_near_any_bound_it_is_held_to_asks_for_the_fine_repeat():
    limits = stresslib.load_limits()
    pairs = _pairs(limits)
    gate = next(i for i, r in enumerate(pairs) if r["class"] == "gate")
    junction = next(i for i, r in enumerate(pairs) if r["class"] == "junction")
    level = pairs[gate]["level"]
    assert level == 5.5
    quiet = [0.0] * len(pairs)
    assert ts.near_limit(pairs, quiet, limits) == []
    for peak in (level - 0.055, level + 0.05,
                 level + limits["gate_oxide"]["repeating"]["peak_over_v"] - 0.03):
        peaks = list(quiet)
        peaks[gate] = peak
        (n,) = ts.near_limit(pairs, peaks, limits)
        assert n["pair"] == pairs[gate]["pair"]
    peaks = list(quiet)
    peaks[junction] = pairs[junction]["breakdown"] - 0.02
    assert [n["pair"] for n in ts.near_limit(pairs, peaks, limits)] \
        == [pairs[junction]["pair"]]


def test_a_coarse_peak_near_the_limit_is_run_again_at_the_fine_step(
        tmp_path, monkeypatch):
    limits = stresslib.load_limits()
    pairs = _pairs(limits)
    ran = []

    def fake_sim(job, stem, text):
        ran.append(stem)
        raw = tmp_path / f"{stem}.raw"
        raw.write_text("x")
        return None, {"data": None, "columns": {}}, raw
    peak = {"c": 5.45, "c__fine": 5.52}
    monkeypatch.setattr(ts, "_simulate", fake_sim)
    monkeypatch.setattr(stresslib, "score_run", lambda *a: [])
    monkeypatch.setattr(stresslib, "with_step", lambda text, step, what: text)
    # the first pair is a gate pair; every other pair is quiet
    assert pairs[0]["class"] == "gate"
    monkeypatch.setattr(stresslib, "pair_peaks", lambda data, cols, prs:
                        [peak[ran[-1]]] + [0.0] * (len(prs) - 1))
    dev = dict(pairs[0]["dev"])
    got = ts.run_cell({"devices": [dev], "limits": limits, "stem": "c",
                       "text": "", "window": None})
    assert ran == ["c", "c__fine"]
    assert got["repeat"]["step_s"] == ts.FINE_STEP_S
    assert got["repeat"]["near_limit"][0]["coarse_peak_v"] == 5.45
    # the run is scored from the fine repeat
    assert got["worst"]["peak_v"] == 5.52
    # far from every bound: one run only
    ran.clear()
    peak["c"] = 4.7
    got = ts.run_cell({"devices": [dev], "limits": limits, "stem": "c",
                       "text": "", "window": None})
    assert ran == ["c"] and got["repeat"] is None


def test_a_failing_run_outranks_a_higher_allowance_only_run():
    def row(status, peak, corner):
        return {"cell": "c", "device": "c/X_M1", "label": "c/X_M1",
                "model": "nfet_05v0", "pair": "Vgs", "class": "gate",
                "limit_v": 5.5, "status": status, "corner": corner,
                "deck": "max load",
                "rows": [{"kind": "repeating", "peak_v": peak, "share": 0.1}]}
    for order in ((row("fail", 5.7, "ff"), row("pass", 5.9, "fs")),
                  (row("pass", 5.9, "fs"), row("fail", 5.7, "ff"))):
        (o,) = ts.over_limit(list(order), {"c": 1})
        assert (o["status"], o["corner"], o["peak_v"]) == ("fail", "ff", 5.7)


def _fail_rows(job, peak=7.1, corner_peak=None):
    dev = job["devices"][0]
    if corner_peak:
        peak = corner_peak.get(job["corner"], peak)
    rows = [{"device": dev["name"], "label": dev["label"],
             "model": dev["model"], "pair": "Vgs", "class": "gate",
             "limit_v": 5.5, "status": "fail",
             "reasons": [f"repeating peak {peak} V"],
             "rows": [{"kind": "repeating", "peak_v": peak, "share": 0.12}]}]
    return {"rows": rows, "repeat": None,
            "worst": {"device": dev["label"], "pair": "Vgs",
                      "class": "gate", "peak_v": peak, "limit_v": 5.5}}


def test_devices_of_the_same_name_in_two_cells_are_two_findings(
        tmp_path, monkeypatch):
    ws = _fake(tmp_path, monkeypatch)
    monkeypatch.setattr(ts, "run_cell", lambda job: _fail_rows(job)
                        if job["cell"] in (BOOT, INV) else
                        {"rows": [], "repeat": None,
                         "worst": {"device": job["devices"][0]["label"],
                                   "pair": "Vgs", "class": "gate",
                                   "peak_v": 1.0, "limit_v": 5.5}})
    code, payload = _run(ws, tmp_path)
    assert code == 1, payload
    assert sorted(v["module"] for v in payload["violations"]) == [BOOT, INV]
    assert sorted(o["device"] for o in payload["over_limit"]) == [
        f"{BOOT}/X_M1", f"{INV}/X_M1"]
    assert sorted(payload["stress"]["devices_failing"]) == [
        f"{BOOT}/X_M1", f"{INV}/X_M1"]


def test_the_finding_names_the_worst_failing_run(tmp_path, monkeypatch):
    ws = _fake(tmp_path, monkeypatch)
    monkeypatch.setattr(ts, "run_cell", lambda job: _fail_rows(
        job, corner_peak={"ff": 6.0, "fs": 7.4, "sf": 6.2})
        if job["cell"] == BOOT else {"rows": [], "repeat": None,
                                     "worst": {"device": "x", "pair": "Vgs",
                                               "class": "gate", "peak_v": 1.0,
                                               "limit_v": 5.5}})
    code, payload = _run(ws, tmp_path)
    (v,) = payload["violations"]
    assert " at fs / " in v["msg"] and "7.4" in v["msg"], v["msg"]


# ------------------------------------------------------------- real ngspice

@pytest.mark.slow
def test_a_planted_bootstrapped_gate_is_caught_and_the_real_cell_is_clean(
        tmp_path, monkeypatch):
    code, payload = _run(_workspace(tmp_path, monkeypatch), tmp_path)
    assert code == 1, payload
    named = {(v["module"], v["kind"]) for v in payload["violations"]}
    assert named == {(BOOT, "device_overstress")}, payload["violations"]
    over = [o for o in payload["over_limit"] if o["cell"] == BOOT]
    assert over and all(o["limit_v"] == 5.5 for o in over)
    worst = max(over, key=lambda o: o["peak_v"])
    assert worst["pair"] in ("Vgs", "Vgd", "Vgb") and worst["peak_v"] > 6.5
    assert not [o for o in payload["over_limit"] if o["cell"] == INV]
    cells = {c["cell"]: c for c in payload["cells"]}
    assert cells[INV]["worst"]["peak_v"] < cells[INV]["worst"]["limit_v"]
    # nothing is left behind but the decks
    assert not list((tmp_path / "blk" / "log" / "top_stress").glob("*.raw"))
