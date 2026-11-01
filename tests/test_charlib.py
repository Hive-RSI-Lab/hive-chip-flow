"""engine/lib/charlib.py (issue #130). The fast half has no simulator: the
liberty reader, the derate and axis rules, the bench's verdicts with ngspice
faked. The `slow` half is real: PDK cells against the shipped lib, planted
faults, a hand ngspice bench, OpenSTA reading the output."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "lib"))
sys.path.insert(0, str(REPO / "engine" / "scripts"))

import charlib  # noqa: E402
import characterize as characterize_cli  # noqa: E402

LIB = "gf180mcu_fd_sc_mcu7t5v0"
TERMS = charlib.Terms(0.5, 0.3, 0.7, 0.5, 0.5)
HEADER = """library(x__tt) {
  delay_model : table_lookup ;
  time_unit : 1ns ;
  voltage_unit : 1V ;
  capacitive_load_unit(1, pf);
  input_threshold_pct_fall : 50 ;
  input_threshold_pct_rise : 50 ;
  output_threshold_pct_fall : 50 ;
  output_threshold_pct_rise : 50 ;
  slew_derate_from_library : 0.5 ;
  slew_lower_threshold_pct_fall : 30 ;
  slew_lower_threshold_pct_rise : 30 ;
  slew_upper_threshold_pct_fall : 70 ;
  slew_upper_threshold_pct_rise : 70 ;
  nom_process : 1 ;
  nom_temperature : 25 ;
  nom_voltage : 3.3 ;
  voltage_map(VDD, 3.3);
  voltage_map(VSS, 0);
  operating_conditions(x__tt) { process : 1 ; temperature : 25 ; voltage : 3.3 ; }
  input_voltage(default) { vil : 0 ; vih : 3.3 ; vimin : 0 ; vimax : 3.3 ; }
  lu_table_template(gone) { variable_1 : input_net_transition ; index_1("1, 2"); }
"""
CELL = """  cell(x__inv) {
    area : 8.78 ;
    leakage_power() { value : "3.3e-05" ; }
    pg_pin(VDD) { voltage_name : VDD ; pg_type : primary_power ; }
    pg_pin(VSS) { voltage_name : VSS ; pg_type : primary_ground ; }
    pin(I) { capacitance : 0.0046 ; direction : input ; max_transition : 8.6 ; }
    pin(ZN) {
      direction : output ;
      function : "(!I)" ;
      max_capacitance : 0.35 ;
      timing() {
        related_pin : "I" ;
        cell_rise(t) {
          index_1("0.1, 0.2");
          index_2("0.01, 0.02, 0.03");
          values("1, 2, 3", \\
                 "4, 5, 6");
        }
      }
    }
  }
"""
LIBTEXT = "/* a comment */\n" + HEADER + CELL + "}\n"


def test_reader_writer_round_trip():
    lib = charlib.parse_liberty(LIBTEXT)
    again = charlib.parse_liberty(charlib.write_liberty(lib))
    cell = again.sub("cell")[0]
    assert cell.args == "x__inv"
    zn = [p for p in cell.sub("pin") if p.args == "ZN"][0]
    tab = zn.sub("timing")[0].sub("cell_rise")[0]
    assert charlib.table(tab) == [[1, 2, 3], [4, 5, 6]]
    assert zn.attr("function") == '"(!I)"'


def test_split_cuts_one_cell_and_refuses_a_missing_one():
    header, cell = charlib.split_library(LIBTEXT, "x__inv")
    assert "delay_model" in header and cell.strip().startswith("cell(x__inv)")
    with pytest.raises(charlib.Refused) as exc:
        charlib.split_library(LIBTEXT, "nope")
    assert exc.value.kind == "cell_not_in_template"


def test_terms_come_from_the_template_and_asymmetry_is_refused():
    header, _ = charlib.split_library(LIBTEXT, "x__inv")
    g = charlib.parse_liberty(header + "}\n")
    assert charlib.terms_of(g) == TERMS
    g.set_attr("slew_lower_threshold_pct_fall", "20")
    with pytest.raises(charlib.Refused) as exc:
        charlib.terms_of(g)
    assert exc.value.kind == "asymmetric_thresholds"


def test_derate_rules_in_and_out():
    # 30-70 of the ramp is L*derate
    ramp = charlib.ramp_of(0.684, TERMS)
    assert ramp * 0.4 == pytest.approx(0.684 * 0.5)
    assert charlib.to_table_transition(0.3, TERMS) == pytest.approx(0.6)
    # the PDK's own driver waveform says the same: 0.6841 -> 30% at 0.2565, 70% at 0.5986
    assert 0.5986 - 0.2565 == pytest.approx(0.6841 * 0.5, rel=1e-3)


def test_corner_header_rewrites_every_voltage():
    header, _ = charlib.split_library(LIBTEXT, "x__inv")
    h = charlib.corner_header(header, "x__ss", 2.97, 125, True)
    text = charlib.write_liberty(h)
    assert "3.3" not in text and "2.97" in text
    assert h.attr("nom_temperature") == "125"
    assert h.attr("time_unit") == '"1ns"'  # lctime crashes on the bare form
    assert not h.sub("lu_table_template")
    assert "voltage_map(VSS, 0)" in text and "voltage_map(VDD, 2.97)" in text


def test_a_round_supply_keeps_its_point():
    # lctime asserts a float: 3.0 V written as "3" crashed it
    header, _ = charlib.split_library(LIBTEXT, "x__inv")
    assert charlib.corner_header(header, "x", 3.0, 25, True).attr("nom_voltage") == "3.0"


def test_bare_cell_keeps_no_number_from_the_other_corner():
    bare = charlib.bare_cell(charlib.parse_liberty(CELL))
    text = charlib.write_liberty(bare)
    for gone in ("leakage_power", "max_transition", "max_capacitance",
                 "capacitance : 0.0046", "cell_rise"):
        assert gone not in text
    assert '"(!I)"' in text and "pg_type" in text


def test_latch_and_tristate_are_refused():
    for body in ("latch(IQ, IQN) { enable : E ; data_in : D ; }",
                 'pin(Z) { three_state : "E" ; direction : output ; }'):
        with pytest.raises(charlib.Refused) as exc:
            charlib.bare_cell(charlib.parse_liberty(f"cell(c) {{ {body} }}"))
        assert exc.value.kind == "unsupported_cell"


def test_netlist_directives_and_ports():
    text = ("* comment\n.SUBCKT child A B\nR1 A B 1\n.ENDS child\n"
            ".SUBCKT top I ZN VDD\n+ VSS\nXc I ZN child\n.ENDS\n.end\n")
    out = charlib.prepare_netlist(text, "top")
    assert ".subckt child A B" in out and ".SUBCKT" not in out
    assert ".ENDS" not in out and ".end\n" not in out.replace(".ends\n", "")
    assert charlib.ports_of(out, "top") == ["I", "ZN", "VDD", "VSS"]
    with pytest.raises(charlib.Refused) as exc:
        charlib.prepare_netlist(text, "other")
    assert exc.value.kind == "cell_not_in_netlist"


def test_extracted_wins_when_it_exists(tmp_path):
    schem, ext = tmp_path / "s.sp", tmp_path / "e.sp"
    schem.write_text("x")
    assert charlib.choose_netlist(ext, schem) == (schem, "schematic")
    ext.write_text("x")
    assert charlib.choose_netlist(ext, schem) == (ext, "extracted")
    ext.write_text("")
    assert charlib.choose_netlist(ext, schem) == (schem, "schematic")
    schem.unlink()
    with pytest.raises(charlib.Refused) as exc:
        charlib.choose_netlist(ext, schem)
    assert exc.value.kind == "no_netlist"


def test_function_evaluator():
    ev = charlib.eval_function
    assert ev('"(!I)"', {"I": False}) is True
    assert ev("A1 & A2", {"A1": True, "A2": False}) is False
    assert ev("A1 A2'", {"A1": True, "A2": False}) is True
    assert ev("(A|B)^C", {"A": False, "B": True, "C": True}) is False
    assert ev("A*B+C", {"A": False, "B": True, "C": True}) is True
    with pytest.raises(charlib.Refused):
        ev("A&Z", {"A": True})


def raw_lctime(rows_load_by_slew):
    """lctime's shape: index_1 load, index_2 ramp (0-100%), raw 30-70 times."""
    def tab(name, rows):
        return (f'{name}(t) {{ index_1("0.01, 0.02"); index_2("0.25, 0.5, 1.25"); '
                f'values({", ".join(chr(34) + ", ".join(map(str, r)) + chr(34) for r in rows)}); }}')
    return ("library(x) { cell(c) { area : 1 ; "
            'pin(I) { direction : input ; capacitance : 0.004 ; } '
            'pin(ZN) { direction : output ; function : "!I" ; '
            'timing() { related_pin : "I" ; timing_sense : negative_unate ; '
            + tab("cell_rise", rows_load_by_slew)
            + tab("rise_transition", rows_load_by_slew)
            + " } internal_power() { related_pin : \"I\" ; } } } }")


def assembled():
    header = charlib.corner_header(HEADER, "x__tt", 3.3, 25, False)
    rows = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
    return charlib.assemble(raw_lctime(rows), header, TERMS,
                            [0.2, 0.4, 1.0], [0.01, 0.02],
                            [charlib.ramp_of(s, TERMS) for s in (0.2, 0.4, 1.0)])


def test_assemble_transposes_derates_and_drops_what_was_not_measured():
    cell, text = assembled()
    timing = [p for p in cell.sub("pin") if p.args == "ZN"][0].sub("timing")[0]
    delay = timing.sub("cell_rise")[0]
    assert charlib.numbers(delay.attr("index_1")) == [0.2, 0.4, 1.0]  # slews
    assert charlib.numbers(delay.attr("index_2")) == [0.01, 0.02]     # loads
    assert charlib.table(delay) == [[0.1, 0.4], [0.2, 0.5], [0.3, 0.6]]
    assert charlib.table(timing.sub("rise_transition")[0])[0] == [0.2, 0.8]
    assert "internal_power" not in text and "gone" not in text
    # what comes out is readable again, with a template of the right size
    again = charlib.parse_liberty(text)
    assert [g.args for g in again.sub("lu_table_template")] == ["charlib_delay"]


def test_assemble_refuses_axes_that_were_not_asked_for():
    header = charlib.corner_header(HEADER, "x__tt", 3.3, 25, False)
    with pytest.raises(charlib.Refused) as exc:
        charlib.assemble(raw_lctime([[1, 2, 3], [4, 5, 6]]), header, TERMS,
                         [0.2, 0.4, 1.0], [0.01, 0.09],
                         [charlib.ramp_of(s, TERMS) for s in (0.2, 0.4, 1.0)])
    assert exc.value.kind == "table_axes"


def test_pick_is_a_subset_of_the_template_only():
    assert charlib.pick([1, 2, 3], [0, 2]) == [1, 3]
    assert charlib.pick([1, 2, 3], None) == [1, 2, 3]
    with pytest.raises(charlib.Refused):
        charlib.pick([1, 2], [5])


def fake_ngspice(monkeypatch, tmp_path, *, delay_ns, raw_ns):
    """An ngspice whose output edge starts where the deck's input threshold is."""
    def run(_eda, deck, _cwd, _timeout):
        text = Path(deck).read_text()
        points = re.search(r"PWL\(([^)]*)\)", text).group(1).split()
        t0, t1 = float(points[2][:-1]), float(points[4][:-1])
        t50 = (t0 + t1) / 2 + delay_ns
        stdout = ("  Measurements for Transient Analysis\n\n"
                  f"t_o50 = {t50 * 1e-9:e}\n"
                  f"t_a = {(t50 - raw_ns / 2) * 1e-9:e}\n"
                  f"t_b = {(t50 + raw_ns / 2) * 1e-9:e}\n")
        return stdout, "", 0
    monkeypatch.setattr(charlib.simlib, "run_ngspice", run)
    net = tmp_path / "n.sp"
    net.write_text(".subckt c I ZN VDD VSS\n.ends\n")
    return net


def validate_fake(tmp_path, cell):
    for pg, kind in (("VDD", "primary_power"), ("VSS", "primary_ground")):
        g = charlib.Group("pg_pin", pg)
        g.set_attr("pg_type", kind)
        cell.groups.append(g)
    return charlib.validate(
        cell, netlist=tmp_path / "n.sp", pdk=tmp_path,
        corner={"name": "tt", "process": "typical", "temp_c": 25}, vdd=3.3,
        terms=TERMS, slews=[0.2, 0.4, 1.0], loads=[0.01, 0.02],
        work=tmp_path / "w", checks=1)


def fall_too(cell):
    timing = [p for p in cell.sub("pin") if p.args == "ZN"][0].sub("timing")[0]
    for name in ("cell_rise", "rise_transition"):
        twin = timing.sub(name)[0]
        other = charlib.Group(name.replace("rise", "fall"), twin.args,
                              list(twin.attrs), [])
        timing.groups.append(other)
    return timing


def test_bench_passes_a_true_table_and_fails_a_halved_transition(
        monkeypatch, tmp_path):
    cell, _ = assembled()
    timing = fall_too(cell)
    for tab in timing.groups:
        tab.set_attr("values", '"0.1, 0.1", "0.1, 0.1", "0.1, 0.1"', True)
        if tab.name.endswith("_transition"):
            tab.set_attr("values", '"0.4, 0.4", "0.4, 0.4", "0.4, 0.4"', True)
    fake_ngspice(monkeypatch, tmp_path, delay_ns=0.1, raw_ns=0.2)
    assert validate_fake(tmp_path, cell)["ok"]
    for tab in timing.groups:
        if tab.name.endswith("_transition"):
            tab.set_attr("values", '"0.2, 0.2", "0.2, 0.2", "0.2, 0.2"', True)
    result = validate_fake(tmp_path, cell)
    assert not result["ok"]
    assert {p["table"] for p in result["failed"]} == {"rise_transition",
                                                      "fall_transition"}


def test_a_measure_that_never_happened_is_a_failed_point(monkeypatch, tmp_path):
    cell, _ = assembled()
    fall_too(cell)
    fake_ngspice(monkeypatch, tmp_path, delay_ns=0.1, raw_ns=0.2)
    monkeypatch.setattr(charlib.simlib, "run_ngspice",
                        lambda *a: ("no measures here\n", "", 0))
    assert not validate_fake(tmp_path, cell)["ok"]


def test_cli_refuses_with_a_remediation(tmp_path, capsys):
    rc = characterize_cli.main(["--cell", "x", "--out", str(tmp_path),
                                "--template-lib", str(tmp_path / "none.lib")])
    report = json.loads(capsys.readouterr().out)
    assert rc == 2 and report["status"] == "error" and report["remediation"]


# --- real tools

def _pdk():
    import layoutlib
    return layoutlib.pdk_root()


def _ref():
    return _pdk() / "libs.ref" / LIB


def _shipped(corner="tt_025C_3v30"):
    return _ref() / "lib" / f"{LIB}__{corner}.lib"


def _extract(cell, work):
    import layoutlib
    path, _ = layoutlib.run_magic_extract(
        work, _ref() / "gds" / f"{LIB}.gds", cell, parasitics=True, timeout=600)
    return path


TT25 = {"name": "tt_025C", "process": "typical", "temp_c": 25, "supply_pct": 0}


def _run(cell, out, *, extracted=None, corner=TT25, slews=(1, 3), loads=(3, 4),
         constraint=None, checks=1, seed=1, template=None):
    return charlib.characterize(
        f"{LIB}__{cell}", template_lib=template or _shipped(), corners=[corner],
        nominal_vdd=3.3, out_dir=out, pdk=_pdk(), extracted=extracted,
        schematic=_ref() / "spice" / f"{LIB}.spice",
        slew_indices=list(slews), load_indices=list(loads),
        constraint_indices=list(constraint or slews), checks=checks, seed=seed)


def _compare(report, cell, shipped=None, corner="tt_025C"):
    lib = charlib.parse_liberty(
        Path(report["corners"][corner]["lib"]).read_text())
    got = lib.sub("cell")[0]
    text = (shipped or _shipped()).read_text(errors="replace")
    _, ship = charlib.split_library(text, f"{LIB}__{cell}")
    return got, charlib.compare_shipped(got, ship), charlib.parse_liberty(ship)


def _band(rows, kinds, load_min, load_max=1e9):
    picked = [r["ratio"] for r in rows if any(k in r["table"] for k in kinds)
              and load_min <= r["load"] < load_max]
    assert picked
    return min(picked), max(picked)


@pytest.mark.slow
@pytest.mark.parametrize("cell", ["inv_1", "nand2_1"])
def test_known_answer_extracted_at_real_loads(cell, tmp_path):
    full = f"{LIB}__{cell}"
    report = _run(cell, tmp_path / "out", extracted=_extract(full, tmp_path))
    assert report["netlist_kind"] == "extracted"
    assert report["ok"], report["corners"]["tt_025C"]["check"]["failed"]
    _, rows, _ = _compare(report, cell)
    for kind, kinds in (("delay", ("cell_",)), ("transition", ("_transition",))):
        low, high = charlib.TOLERANCE_SHIPPED[("extracted", kind, "load>=20fF")]
        got = _band(rows, kinds, 0.020)
        assert low <= got[0] and got[1] <= high, (kind, got)


@pytest.mark.slow
def test_known_answer_schematic_by_load_range(tmp_path):
    report = _run("inv_1", tmp_path / "out", loads=(1, 3))
    assert report["netlist_kind"] == "schematic" and report["ok"]
    _, rows, _ = _compare(report, "inv_1")
    for key, kinds, lo_load, hi_load in (
            (("schematic", "delay", "load>=20fF"), ("cell_",), 0.02, 1e9),
            (("schematic", "delay", "load<20fF"), ("cell_",), 0, 0.02),
            (("schematic", "transition", "load>=20fF"), ("_transition",), 0.02, 1e9)):
        low, high = charlib.TOLERANCE_SHIPPED[key]
        got = _band(rows, kinds, lo_load, hi_load)
        assert low <= got[0] and got[1] <= high, (key, got)


@pytest.mark.slow
def test_slow_corner_lands_on_the_shipped_slow_lib(tmp_path):
    """Process, temperature and supply all move: ss, 125 C, 3.0 V. Without the
    temperature card this reads 25 C numbers."""
    corner = {"name": "ss_125C", "process": "ss", "temp_c": 125,
              "supply_pct": (3.0 / 3.3 - 1) * 100}
    full = f"{LIB}__inv_1"
    report = _run("inv_1", tmp_path / "out", corner=corner, slews=(1, 3), loads=(3,),
                  extracted=_extract(full, tmp_path),
                  template=_shipped("ss_125C_3v00"))
    assert report["ok"]
    _, rows, _ = _compare(report, "inv_1", shipped=_shipped("ss_125C_3v00"),
                          corner="ss_125C")
    assert all(0.75 <= r["ratio"] <= 1.25 for r in rows), rows


@pytest.mark.slow
def test_planted_no_derate_is_caught(tmp_path, monkeypatch):
    monkeypatch.setattr(charlib, "to_table_transition", lambda raw, _t: raw)
    report = _run("inv_1", tmp_path / "out", slews=(1,), loads=(3,))
    assert not report["ok"]
    check = report["corners"]["tt_025C"]["check"]
    assert {p["table"] for p in check["failed"]} == {"rise_transition",
                                                     "fall_transition"}
    for p in check["failed"]:
        assert 0.4 < p["value_ns"] / p["bench_ns"] < 0.6
    path = Path(report["corners"]["tt_025C"]["lib"])
    assert path.name.endswith(".rejected.lib")
    assert not path.with_name(path.name.replace(".rejected", "")).exists()


@pytest.mark.slow
def test_planted_wrong_temperature_is_caught(tmp_path, monkeypatch):
    monkeypatch.setattr(charlib, "temperature_card", lambda _c: "")
    hot = {"name": "hot", "process": "typical", "temp_c": 125, "supply_pct": 0}
    report = _run("inv_1", tmp_path / "out", corner=hot, slews=(1,), loads=(1,))
    assert not report["ok"]


@pytest.mark.slow
def test_hand_bench_agrees_with_the_table(tmp_path):
    """One point, a deck written out here by hand with no charlib helper:
    inv_1, tt, 25 C, library slew 0.2871 ns, 28.04 fF, input falling."""
    report = _run("inv_1", tmp_path / "out", slews=(2,), loads=(3,))
    assert report["ok"]
    cell = charlib.parse_liberty(
        Path(report["corners"]["tt_025C"]["lib"]).read_text()).sub("cell")[0]
    timing = [p for p in cell.sub("pin") if p.args == "ZN"][0].sub("timing")[0]
    table_delay = charlib.table(timing.sub("cell_rise")[0])[0][0]
    table_trans = charlib.table(timing.sub("rise_transition")[0])[0][0]
    ramp = 0.2871 * 0.5 / 0.4
    m = _pdk() / "libs.tech" / "ngspice"
    deck = tmp_path / "hand.cir"
    deck.write_text(f"""* hand bench
.lib {m}/sm141064.ngspice typical
.include {m}/design.ngspice
.temp 25
.subckt inv I ZN VDD VNW VPW VSS
X_i_0 ZN I VSS VPW nfet_05v0 W=8.2e-07 L=6e-07
X_i_1 ZN I VDD VNW pfet_05v0 W=1.22e-06 L=5e-07
.ends
Xd i zn vdd vdd 0 0 inv
Vdd vdd 0 3.3
Vi i 0 PWL(0 3.3 1n 3.3 {1 + ramp}n 0 20n 0)
Cl zn 0 {_ref_load()}p
.tran 0.5p 8n
.meas tran t50 WHEN v(zn)=1.65 RISE=1
.meas tran t30 WHEN v(zn)=0.99 RISE=1
.meas tran t70 WHEN v(zn)=2.31 RISE=1
.end
""")
    eda = REPO / "bin" / "eda"
    run = subprocess.run([str(eda), "ngspice", "-b", str(deck)], cwd=tmp_path,
                         capture_output=True, text=True, timeout=300)
    got = {k: float(v) for k, v in re.findall(
        r"^\s*(t\d+)\s*=\s*([-+.\deE]+)", run.stdout, re.M)}
    delay = got["t50"] * 1e9 - (1 + ramp / 2)
    trans = (got["t70"] - got["t30"]) * 1e9 / 0.5
    assert table_delay == pytest.approx(delay, rel=0.03)
    assert table_trans == pytest.approx(trans, rel=0.03)


def _ref_load():
    text = _shipped().read_text(errors="replace")
    _, cell = charlib.split_library(text, f"{LIB}__inv_1")
    return charlib.index_points(charlib.parse_liberty(cell))["loads"][3]


@pytest.mark.slow
def test_opensta_reads_the_output(tmp_path):
    report = _run("inv_1", tmp_path / "out", slews=(1, 3), loads=(1, 3))
    assert report["ok"]
    lib = report["corners"]["tt_025C"]["lib"]
    tcl = tmp_path / "s.tcl"
    net = tmp_path / "n.v"
    full = f"{LIB}__inv_1"
    net.write_text(f"module t(input a, output y); {full} u(.I(a), .ZN(y)); endmodule\n")
    tcl.write_text(f"read_liberty {lib}\nread_verilog {net}\nlink_design t\n"
                   "set_input_delay 0 [get_ports a]\nset_input_transition 0.3 [get_ports a]\n"
                   "set_load 0.02 [get_ports y]\nset_output_delay 0 [get_ports y]\n"
                   "report_checks -unconstrained\nexit\n")
    eda = REPO / "bin" / "eda"
    run = subprocess.run([str(eda), "sta", str(tcl)], cwd=tmp_path,
                         capture_output=True, text=True, timeout=300)
    out = run.stdout + run.stderr
    assert "Error" not in out and "Warning" not in out, out
    assert full in out and "data arrival time" in out


@pytest.mark.slow
def test_dffq_extracted_clock_to_q_setup_hold(tmp_path):
    """lctime can do dffq_1 once its min-pulse search is skipped (lctime_shim).
    Clock-to-Q and transitions land in the band. Setup and hold pass the bench's
    two-sided check but are pessimistic against the shipped lib: asserted as
    what they are, safe-side at every point, with no accuracy claim."""
    cell = "dffq_1"
    full = f"{LIB}__{cell}"
    report = _run(cell, tmp_path / "out", extracted=_extract(full, tmp_path),
                  slews=(1, 3), loads=(3,))
    assert report["ok"], report["corners"]["tt_025C"]["check"]["failed"]
    got, rows, ship = _compare(report, cell)
    for kind, kinds in (("delay", ("cell_",)), ("transition", ("_transition",))):
        low, high = charlib.TOLERANCE_SHIPPED[("extracted", kind, "load>=20fF")]
        band = _band(rows, kinds, 0.020)
        assert low <= band[0] and band[1] <= high, (kind, band)
    clock = [p for p in got.sub("pin") if p.args == "CLK"][0]
    assert clock.attr("min_pulse_width_high") is None  # never measured
    low, high = charlib.TOLERANCE_SHIPPED[("extracted", "input_cap", "any")]
    for name in ("CLK", "D"):
        mine = float([p for p in got.sub("pin") if p.args == name][0].attr("capacitance"))
        theirs = float([p for p in ship.sub("pin") if p.args == name][0].attr("capacitance"))
        assert low <= mine / theirs <= high, name
    data = [p for p in got.sub("pin") if p.args == "D"][0]
    ship_d = [p for p in ship.sub("pin") if p.args == "D"][0]
    kinds = {t.attr("timing_type") for t in data.sub("timing")}
    assert kinds == {"setup_rising", "hold_rising"}
    for timing in data.sub("timing"):
        twin = [t for t in ship_d.sub("timing")
                if t.attr("timing_type") == timing.attr("timing_type")][0]
        for tab in timing.groups:
            other = twin.sub(tab.name)[0]
            for i, s_ in enumerate(charlib.numbers(tab.attr("index_1"))):
                for j, c_ in enumerate(charlib.numbers(tab.attr("index_2"))):
                    assert charlib.table(tab)[i][j] >= charlib._lookup(other, s_, c_)


@pytest.mark.slow
def test_planted_optimistic_setup_is_caught(tmp_path):
    """The table's setup 0.3 ns short: the bench's capture at table+margin fails."""
    cell = "dffq_1"
    report = _run(cell, tmp_path / "out", slews=(1,), loads=(3,))
    lib = charlib.parse_liberty(
        Path(report["corners"]["tt_025C"]["lib"]).read_text())
    got = lib.sub("cell")[0]
    data = [p for p in got.sub("pin") if p.args == "D"][0]
    setup = [t for t in data.sub("timing")
             if t.attr("timing_type") == "setup_rising"][0]
    for tab in setup.groups:
        rows = [[v - 0.3 for v in r] for r in charlib.table(tab)]
        tab.set_attr("values", charlib._quoted_rows(rows), True)
    work = next((tmp_path / "out").glob("*.work"))
    check = charlib.validate(
        got, netlist=work / "netlist.sp", pdk=_pdk(), corner=TT25, vdd=3.3,
        terms=TERMS, slews=report["slews"], loads=report["loads"],
        data_slews=report["slews"], clock_slews=report["constraint_slews"],
        work=tmp_path / "again", checks=1, seed=1)
    assert not check["ok"]
    assert any("setup" in p["table"] and p["check"] == "not_optimistic"
               for p in check["failed"])


@pytest.mark.slow
def test_cli_writes_one_lib_per_default_corner(tmp_path, capsys):
    """corners.yaml's default five, one point each. The corners must differ."""
    rc = characterize_cli.main(
        ["--cell", f"{LIB}__inv_1", "--out", str(tmp_path / "out"),
         "--slew-index", "1", "--load-index", "3", "--checks", "1"])
    report = json.loads(capsys.readouterr().out)
    assert rc == 0, report.get("violations") or report
    assert set(report["corners"]) == {"tt", "ss", "ff", "sf", "fs"}
    delays = {}
    for name, corner in report["corners"].items():
        path = Path(corner["lib"])
        assert path.name == f"{LIB}__inv_1__{name}.lib"
        lib = charlib.parse_liberty(path.read_text())
        assert float(lib.attr("nom_voltage")) == pytest.approx(corner["vdd"])
        zn = [p for p in lib.sub("cell")[0].sub("pin") if p.args == "ZN"][0]
        delays[name] = charlib.table(zn.sub("timing")[0].sub("cell_rise")[0])[0][0]
    assert delays["ff"] < delays["tt"] < delays["ss"]
