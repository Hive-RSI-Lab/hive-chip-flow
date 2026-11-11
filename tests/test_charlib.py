"""engine/lib/charlib.py (issue #130). The fast half has no simulator: the
liberty reader, the derate and axis rules, the bench's verdicts with ngspice
faked. The `slow` half is real: PDK cells against the shipped lib, planted
faults, a hand ngspice bench, OpenSTA reading the output."""
from __future__ import annotations

import json
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
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
    assert [g.args for g in again.sub("lu_table_template")] == \
        ["charlib_delay_3x2"]


def _fake_power(slews, loads):
    rows = [[0.01 * (i + j + 1) for j in range(len(loads))]
            for i in range(len(slews))]
    return {"slews": slews, "loads": loads,
            "arcs": [{"pin": "ZN", "related_pin": "I", "when": "",
                      "rise": rows, "fall": rows}],
            "hidden": [],
            "leakage": {"states": [{"when": "I", "value_uw": 2e-5},
                                   {"when": "!I", "value_uw": 5e-5}],
                        "cell_uw": 5e-5}}


def test_power_leakage_and_limits_land_where_sta_reads_them():
    lib = charlib.parse_liberty(charlib.assemble(
        raw_lctime([[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]),
        charlib.corner_header(HEADER, "x__tt", 3.3, 25, False), TERMS,
        [0.2, 0.4, 1.0], [0.01, 0.02],
        [charlib.ramp_of(s, TERMS) for s in (0.2, 0.4, 1.0)])[1])
    cell = lib.sub("cell")[0]
    if not cell.sub("pg_pin"):
        cell.groups.insert(0, charlib.parse_liberty(
            "pg_pin(VDD) { pg_type : primary_power ; }"))
    charlib.install_power(lib, cell, _fake_power([0.2, 0.4, 1.0], [0.01, 0.02]))
    limits = charlib.install_limits(cell, [0.2, 0.4, 1.0], [0.01, 0.02])
    assert limits == {"max_transition": 1.0, "max_capacitance": 0.02}
    again = charlib.parse_liberty(charlib.write_liberty(lib))
    cell = again.sub("cell")[0]
    pins = {p.args: p for p in cell.sub("pin")}
    assert pins["I"].attr("max_transition") == "1"
    assert pins["I"].attr("max_capacitance") is None
    assert pins["ZN"].attr("max_capacitance") == "0.02"
    ip = pins["ZN"].sub("internal_power")[0]
    rise = ip.sub("rise_power")[0]
    assert rise.args == "charlib_power_3x2"
    assert charlib.table(rise)[2] == [0.03, 0.04]
    assert [g.args for g in again.sub("power_lut_template")] == \
        ["charlib_power_3x2", "charlib_power_3"]
    leak = cell.sub("leakage_power")
    assert [g.attr("when") for g in leak] == ['"I"', '"!I"']
    assert leak[0].attr("related_pg_pin") == "VDD"
    assert cell.attr("cell_leakage_power") == "5e-05"


def _one_cell_lib(cell, header=HEADER, template="t"):
    body = (f"  lu_table_template({template}) {{ variable_1 : "
            f"input_net_transition ; index_1(\"1, 2\"); }}\n")
    return (header.replace("  lu_table_template(gone) { variable_1 : "
                           "input_net_transition ; index_1(\"1, 2\"); }\n",
                           body)
            + CELL.replace("x__inv", cell) + "}\n")


def test_merge_keeps_one_header_and_template_and_every_cell():
    merged = charlib.parse_liberty(charlib.merge_libraries(
        [_one_cell_lib("a"), _one_cell_lib("b")], "both"))
    assert merged.args == "both"
    assert [g.args for g in merged.sub("cell")] == ["a", "b"]
    assert [g.args for g in merged.sub("lu_table_template")] == ["t"]
    assert len(merged.sub("operating_conditions")) == 1


@pytest.mark.parametrize("second,kind", [
    (_one_cell_lib("b", HEADER.replace("nom_voltage : 3.3", "nom_voltage : 3")),
     "corner_mismatch"),
    (_one_cell_lib("b", HEADER.replace("temperature : 25 ;",
                                       "temperature : 125 ;")),
     "corner_mismatch"),
    (_one_cell_lib("a"), "duplicate_cell"),
    (_one_cell_lib("b").replace('index_1("1, 2")', 'index_1("1, 3")'),
     "template_conflict"),
])
def test_merge_refuses_what_is_not_one_corner(second, kind):
    with pytest.raises(charlib.Refused) as exc:
        charlib.merge_libraries([_one_cell_lib("a"), second], "both")
    assert exc.value.kind == kind


def test_merge_cli_exit_codes(tmp_path, capsys):
    import merge_lib
    a, b = tmp_path / "a.lib", tmp_path / "b.lib"
    a.write_text(_one_cell_lib("a"))
    b.write_text(_one_cell_lib("b"))
    out = tmp_path / "m.lib"
    assert merge_lib.main(["--out", str(out), "--name", "m", str(a),
                           str(b)]) == 0
    assert json.loads(capsys.readouterr().out)["cells"] == ["a", "b"]
    assert merge_lib.main(["--out", str(out), "--name", "m", str(a),
                           str(a)]) == 2
    report = json.loads(capsys.readouterr().out)
    assert report["kind"] == "duplicate_cell" and report["remediation"]


def test_assemble_refuses_axes_that_were_not_asked_for():
    header = charlib.corner_header(HEADER, "x__tt", 3.3, 25, False)
    with pytest.raises(charlib.Refused) as exc:
        charlib.assemble(raw_lctime([[1, 2, 3], [4, 5, 6]]), header, TERMS,
                         [0.2, 0.4, 1.0], [0.01, 0.09],
                         [charlib.ramp_of(s, TERMS) for s in (0.2, 0.4, 1.0)])
    assert exc.value.kind == "table_axes"


def test_a_rejected_rerun_leaves_no_earlier_lib_behind(tmp_path):
    good, bad = tmp_path / "c__tt.lib", tmp_path / "c__tt.rejected.lib"
    assert charlib._write_verdict(tmp_path, "c__tt", True, "first") == good
    assert charlib._write_verdict(tmp_path, "c__tt", False, "second") == bad
    assert not good.exists() and bad.read_text() == "second"
    assert charlib._write_verdict(tmp_path, "c__tt", True, "third") == good
    assert not bad.exists() and good.read_text() == "third"


def test_bisect_converges_and_refuses_a_bad_bracket():
    assert charlib._bisect(lambda x: x >= 0.123, -1.0, 2.0, 0.001, "x") == \
        pytest.approx(0.123, abs=0.001)
    with pytest.raises(charlib.Refused):
        charlib._bisect(lambda x: False, -1.0, 2.0, 0.001, "never")
    with pytest.raises(charlib.Refused):
        charlib._bisect(lambda x: True, -1.0, 2.0, 0.001, "always")


@pytest.mark.parametrize("first, second, ramp", [
    (1.0, 3.0, 0.5), (1.0, 1.2, 0.5), (1.0, 0.8, 0.5), (1.0, 1.0, 0.5)])
def test_pulse_points_width_is_threshold_to_threshold(first, second, ramp):
    """At a 50% threshold the crossings are second - first apart, a triangle
    included; the times keep increasing."""
    vdd = 3.3
    for rest_high in (False, True):
        pts = charlib._pulse_points(vdd, rest_high, first, second, ramp)
        times = [t for t, _ in pts]
        assert times == sorted(times) and len(set(times)) == len(times)

        def crossings():
            out = []
            for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
                if (v0 - vdd / 2) * (v1 - vdd / 2) < 0:
                    out.append(t0 + (vdd / 2 - v0) * (t1 - t0) / (v1 - v0))
            return out
        c = crossings()
        if second > first:
            assert len(c) == 2 and c[1] - c[0] == pytest.approx(second - first)
        else:
            assert c == []


def test_pulse_points_nothing_when_it_never_leaves_the_rail():
    assert charlib._pulse_points(3.3, False, 1.0, 0.4, 0.5) == []


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
    _assert_power_band(report, cell)


def _power_groups(cell):
    out = {}
    for pin in cell.sub("pin"):
        for ip in pin.sub("internal_power"):
            key = (pin.args, (ip.attr("related_pin") or "").strip('"'),
                   (ip.attr("when") or "").strip('"'))
            for g in ip.groups:
                out[key + (g.name,)] = g
    return out


def _assert_power_band(report, cell):
    got, _, ship = _compare(report, cell)
    band = charlib.POWER_BAND_SHIPPED
    ours, theirs = _power_groups(got), _power_groups(ship)
    assert ours and set(ours) <= set(theirs), set(ours) - set(theirs)
    for key, g in ours.items():
        s = theirs[key]
        tol = band["hidden" if g.attr("index_2") is None else "output"]
        slews = charlib.numbers(g.attr("index_1"))
        if g.attr("index_2") is None:
            ship_idx = charlib.numbers(s.attr("index_1"))
            pairs = [(v, charlib.table(s)[0][charlib._nearest(ship_idx, sl)])
                     for sl, v in zip(slews, charlib.table(g)[0])]
        else:
            loads = charlib.numbers(g.attr("index_2"))
            pairs = [(charlib.table(g)[i][j], charlib._lookup(s, sl, ld))
                     for i, sl in enumerate(slews) for j, ld in enumerate(loads)]
        for v, sv in pairs:
            assert abs(v - sv) <= max(tol["rel"] * abs(sv), tol["abs_pj"]), (
                key, v, sv)
    low, high = band["leakage_ratio"]
    shipped = {(g.attr("when") or "").strip('"'): float(g.attr("value").strip('"'))
               for g in ship.sub("leakage_power")}
    states = report["corners"]["tt_025C"]["power"]["leakage"]["states"]
    assert len(states) == 2 ** len(
        [p for p in got.sub("pin") if p.attr("direction") == "input"])
    for row in states:
        ratio = row["value_uw"] / shipped[row["when"]]
        assert low < ratio <= high, (row, shipped[row["when"]])


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
def test_planted_load_energy_left_in_is_caught(tmp_path, monkeypatch):
    """Internal power that keeps the load's C*V^2 fails the power check at the
    largest load, on the rising output only: a falling one draws nothing
    from the supply for its load."""
    monkeypatch.setattr(charlib, "_load_energy", lambda *_a: 0.0)
    report = _run("inv_1", tmp_path / "out", slews=(1,), loads=(4,))
    assert not report["ok"]
    failed = report["corners"]["tt_025C"]["check"]["failed"]
    assert failed and {p["table"] for p in failed} == {"rise_power"}, failed
    for p in failed:
        assert p["value_pj"] - p["bench_pj"] == pytest.approx(
            3.3 * 3.3 * p["load"], rel=0.05)


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
    power = [p for p in cell.sub("pin") if p.args == "ZN"][0].sub(
        "internal_power")[0]
    table_rise_pj = charlib.table(power.sub("rise_power")[0])[0][0]
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
.tran 0.5p 13n
.meas tran t50 WHEN v(zn)=1.65 RISE=1
.meas tran t30 WHEN v(zn)=0.99 RISE=1
.meas tran t70 WHEN v(zn)=2.31 RISE=1
.meas tran q INTEG i(vdd) FROM=0.95n TO=12n
.meas tran v0 FIND v(zn) AT=0.95n
.meas tran v1 FIND v(zn) AT=12n
.end
""")
    eda = REPO / "bin" / "eda"
    run = subprocess.run([str(eda), "ngspice", "-b", str(deck)], cwd=tmp_path,
                         capture_output=True, text=True, timeout=300)
    got = {k: float(v) for k, v in re.findall(
        r"^\s*(t\d+|q|v\d)\s*=\s*([-+.\deE]+)", run.stdout, re.M)}
    delay = got["t50"] * 1e9 - (1 + ramp / 2)
    trans = (got["t70"] - got["t30"]) * 1e9 / 0.5
    assert table_delay == pytest.approx(delay, rel=0.03)
    assert table_trans == pytest.approx(trans, rel=0.03)
    # internal power: the supply's charge integral less what went into the load
    # (C*V*dV on a rising output), in pJ
    internal = (-3.3 * got["q"] - 3.3 * _ref_load() * 1e-12
                * (got["v1"] - got["v0"])) * 1e12
    assert table_rise_pj == pytest.approx(
        internal, rel=charlib.POWER_TOLERANCE["rel"],
        abs=charlib.POWER_TOLERANCE["abs_pj"])


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
def test_opensta_flags_overload_with_the_merged_lib(tmp_path):
    """inv_1 and nand2_1 merged into one corner lib. A net loaded past the
    largest characterised load and driven past the largest slew is a
    max_capacitance and a max_slew violation; a light one is neither."""
    libs = []
    for cell in ("inv_1", "nand2_1"):
        report = _run(cell, tmp_path / cell, slews=(1, 3), loads=(1, 3))
        assert report["ok"], report["corners"]["tt_025C"]["check"]["failed"]
        libs.append(report["corners"]["tt_025C"]["lib"])
        limits = report["corners"]["tt_025C"]["limits"]
    merged = tmp_path / "merged.lib"
    import merge_lib
    assert merge_lib.main(["--out", str(merged), "--name", "both"] + libs) == 0
    net = tmp_path / "n.v"
    net.write_text(
        f"module t(input a, input b, output y, output z);\n"
        f"wire n; {LIB}__nand2_1 u1(.A1(a), .A2(b), .ZN(n));\n"
        f"{LIB}__inv_1 u2(.I(n), .ZN(y));\n"
        f"{LIB}__inv_1 u3(.I(b), .ZN(z));\nendmodule\n")
    eda = REPO / "bin" / "eda"

    def checks(load, slew):
        tcl = tmp_path / "s.tcl"
        tcl.write_text(
            f"read_liberty {merged}\nread_verilog {net}\nlink_design t\n"
            f"set_input_transition {slew} [get_ports a]\n"
            "set_input_transition 0.1 [get_ports b]\n"
            f"set_load {load} [get_ports y]\nset_load 0.005 [get_ports z]\n"
            "report_check_types -max_capacitance -max_slew -violators\nexit\n")
        run = subprocess.run([str(eda), "sta", str(tcl)], cwd=tmp_path,
                             capture_output=True, text=True, timeout=300)
        out = run.stdout + run.stderr
        assert "Error" not in out, out
        return out
    light = checks(0.5 * limits["max_capacitance"],
                   0.5 * limits["max_transition"])
    assert "VIOLATED" not in light, light
    heavy = checks(3 * limits["max_capacitance"],
                   3 * limits["max_transition"])
    lines = [ln for ln in heavy.splitlines() if "VIOLATED" in ln]
    assert any(ln.split()[0] == "u2/ZN" for ln in lines), heavy
    assert any(ln.split()[0] == "u1/A1" for ln in lines), heavy
    assert not any(ln.split()[0].startswith("u3") for ln in lines), heavy


@pytest.mark.slow
def test_dffq_extracted_clock_to_q_setup_hold(tmp_path):
    """Extracted dffq_1 against the shipped tt lib (issue #172): clock-to-Q and
    transitions in their ratio band; setup and hold, bisected, within an
    absolute band at every point (slews up to 0.7 ns); min_pulse_width high
    and low present and in their ratio band."""
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
    low, high = charlib.TOLERANCE_SHIPPED[("extracted", "input_cap", "any")]
    for name in ("CLK", "D"):
        mine = float([p for p in got.sub("pin") if p.args == name][0].attr("capacitance"))
        theirs = float([p for p in ship.sub("pin") if p.args == name][0].attr("capacitance"))
        assert low <= mine / theirs <= high, name
    data = [p for p in got.sub("pin") if p.args == "D"][0]
    kinds = {t.attr("timing_type") for t in data.sub("timing")}
    assert kinds == {"setup_rising", "hold_rising"}
    cons = charlib.compare_constraints(
        got, charlib.split_library(_shipped().read_text(errors="replace"), full)[1])
    diffs = [r for r in cons if "diff" in r]
    assert {r["table"] for r in diffs} == {"setup_rise", "setup_fall",
                                          "hold_rise", "hold_fall"}
    assert len(diffs) == 4 * 2 * 2
    low, high = charlib.TOLERANCE_SHIPPED[
        ("extracted", "setup_hold_abs_ns", "slew<=0.7ns")]
    for r in diffs:
        assert r["data_slew"] <= 0.7 and r["clock_slew"] <= 0.7
        assert low <= r["diff"] <= high, r
    low, high = charlib.TOLERANCE_SHIPPED[("extracted", "min_pulse_width", "any")]
    widths = {r["table"]: r["ratio"] for r in cons if "ratio" in r}
    assert set(widths) == {"min_pulse_width_high", "min_pulse_width_low"}
    for name, ratio in widths.items():
        assert low <= ratio <= high, (name, ratio)


@pytest.mark.slow
@pytest.mark.parametrize("table_type, attr, shift, caught", [
    ("setup_rising", None, -0.3, "not_optimistic"),
    ("setup_rising", None, +0.3, "not_pessimistic"),
    ("hold_rising", None, +0.3, "not_pessimistic"),
    (None, "min_pulse_width_high", -0.3, "not_optimistic"),
])
def test_planted_constraint_fault_is_caught(tmp_path, table_type, attr, shift,
                                            caught):
    """A table 0.3 ns off either way fails the two-sided check: short, the
    flop misses at table + margin; long, it still captures at table - margin."""
    cell = "dffq_1"
    report = _run(cell, tmp_path / "out", slews=(1,), loads=(3,))
    assert report["ok"], report["corners"]["tt_025C"]["check"]["failed"]
    lib = charlib.parse_liberty(
        Path(report["corners"]["tt_025C"]["lib"]).read_text())
    got = lib.sub("cell")[0]
    if attr:
        clock = [p for p in got.sub("pin") if p.args == "CLK"][0]
        clock.set_attr(attr, f"{float(clock.attr(attr)) + shift:.4g}")
        name = attr
    else:
        data = [p for p in got.sub("pin") if p.args == "D"][0]
        timing = [t for t in data.sub("timing")
                  if t.attr("timing_type") == table_type][0]
        for tab in timing.groups:
            rows = [[v + shift for v in r] for r in charlib.table(tab)]
            tab.set_attr("values", charlib._quoted_rows(rows), True)
        name = table_type.split("_")[0]
    work = next((tmp_path / "out").glob("*.work"))
    check = charlib.validate(
        got, netlist=work / "netlist.sp", pdk=_pdk(), corner=TT25, vdd=3.3,
        terms=TERMS, slews=report["slews"], loads=report["loads"],
        data_slews=report["slews"], clock_slews=report["constraint_slews"],
        work=tmp_path / "again", checks=1, seed=1)
    assert not check["ok"]
    assert any(p["table"].startswith(name) and p["check"] == caught
               for p in check["failed"]), check["failed"]


# An independent bench for setup, hold and min_pulse_width (issue #172): its own
# decks, waveforms, measures and bisection, no charlib bench helper, and the
# criterion stated here, so a change to charlib's cannot follow itself.
HAND_PUSHOUT = 1.03
HAND_RESOLUTION = 0.001
HAND_BAND = 0.20
HAND_VDD = 3.3


def _hand_pulse(start, width, ramp, rest, other):
    """PWL points of a pulse off `rest`, its 50% crossings `width` apart. One
    narrower than its ramp is a triangle that stops short of `other`."""
    if width >= ramp:
        return [(start, rest), (start + ramp, other), (start + width, other),
                (start + width + ramp, rest)]
    peak = rest + (other - rest) * (width + ramp) / (2 * ramp)
    return [(start, rest), (start + (width + ramp) / 2, peak),
            (start + width + ramp, rest)]


def _hand_pwl(points, first, last, stop):
    pairs = [(0, first)] + points + [(stop, last)]
    return "PWL(" + " ".join(f"{t:.6f}n {v:.6f}" for t, v in pairs) + ")"


def _hand_q_delay(work, subckt, clk, d, t_clk, t_from, stop, new_high):
    """Clock's 50% to Q's first 50% crossing the right way after t_from. None
    if Q never crossed or does not end at the new value."""
    m = _pdk() / "libs.tech" / "ngspice"
    edge = "RISE" if new_high else "FALL"
    deck = work / "hand.cir"
    deck.write_text(f"""* hand flop bench
.lib {m}/sm141064.ngspice typical
.include {m}/design.ngspice
.temp 25
{subckt}
Xd d clk q vdd vdd 0 0 {LIB}__dffq_1
Vdd vdd 0 {HAND_VDD}
Vc clk 0 {clk}
Vd d 0 {d}
.tran 1p {stop:.6f}n 0 1p
.meas tran tq WHEN v(q)={HAND_VDD / 2} {edge}=1 TD={t_from:.6f}n
.meas tran vend FIND v(q) AT={stop - 0.5:.6f}n
.end
""")
    run = subprocess.run([str(REPO / "bin" / "eda"), "ngspice", "-b", str(deck)],
                         cwd=work, capture_output=True, text=True, timeout=300)
    assert run.returncode == 0, run.stdout + run.stderr
    got = {k: float(v) for k, v in re.findall(
        r"^\s*(tq|vend)\s*=\s*([-+.\deE]+)", run.stdout, re.M)}
    assert "vend" in got, run.stdout + run.stderr
    if "tq" not in got or (got["vend"] > HAND_VDD / 2) != new_high:
        return None
    return got["tq"] * 1e9 - t_clk


def _hand_window_delay(work, subckt, new_high, setup, hold, cr, dr):
    """D holds the new value only from `setup` before the measured clock edge
    to `hold` after it, each between 50% crossings; an earlier edge loads the
    old value."""
    old, new = (0.0, HAND_VDD) if new_high else (HAND_VDD, 0.0)
    t_clk = 20 + cr / 2
    stop = t_clk + max(hold, 0) + dr + 8
    clk = _hand_pwl(_hand_pulse(6, 4, cr, 0.0, HAND_VDD) +
                    [(20, 0.0), (20 + cr, HAND_VDD)], 0.0, HAND_VDD, stop)
    d = _hand_pwl(_hand_pulse(t_clk - setup - dr / 2, setup + hold, dr, old, new),
                  old, old, stop)
    return _hand_q_delay(work, subckt, clk, d, t_clk, 20 - cr, stop, new_high)


def _hand_width_delay(work, subckt, high, new_high, width, cr, dr):
    """The rising edge that loads the new value over the old, its own high
    phase (`high`) or the low phase before it `width` wide."""
    old, new = (0.0, HAND_VDD) if new_high else (HAND_VDD, 0.0)
    if high:
        t_clk, d_at = 20 + cr / 2, 14
        stop = t_clk + width + cr + 10
        points = (_hand_pulse(4, 3, cr, 0.0, HAND_VDD) +
                  _hand_pulse(20, width, cr, 0.0, HAND_VDD))
        clk = _hand_pwl(points, 0.0, 0.0, stop)
    else:
        t_clk, d_at = 20 + cr / 2 + width, 10
        stop = t_clk + cr + 10
        points = ([(4, 0.0), (4 + cr, HAND_VDD)] +
                  _hand_pulse(20, width, cr, HAND_VDD, 0.0))
        clk = _hand_pwl(points, 0.0, HAND_VDD, stop)
    d = _hand_pwl([(d_at, old), (d_at + dr, new)], old, new, stop)
    return _hand_q_delay(work, subckt, clk, d, t_clk, 20, stop, new_high)


def _hand_bisect(delay, lo, hi, wide):
    """The smallest value in [lo, hi] at which Q captures within HAND_PUSHOUT
    of its delay at `wide`."""
    ref = delay(wide)
    assert ref is not None, "Q never moved with a wide-open window"

    def good(v):
        got = delay(v)
        return got is not None and got <= HAND_PUSHOUT * ref
    assert good(hi) and not good(lo), (lo, hi)
    while hi - lo > HAND_RESOLUTION:
        mid = (lo + hi) / 2
        lo, hi = (lo, mid) if good(mid) else (mid, hi)
    return hi


@pytest.mark.slow
def test_setup_hold_and_pulse_width_match_an_independent_bisection(tmp_path):
    """dffq_1, tt, 25 C, one slew point: every setup, hold and min_pulse_width
    charlib wrote is within HAND_BAND of the same search run on a separate
    bench. charlib's own self-check shares its stimulus and measure with the
    code it checks; this one shares neither."""
    full = f"{LIB}__dffq_1"
    report = _run("dffq_1", tmp_path / "out", slews=(2,), loads=(3,))
    assert report["ok"], report["corners"]["tt_025C"]["check"]["failed"]
    cell = charlib.parse_liberty(
        Path(report["corners"]["tt_025C"]["lib"]).read_text()).sub("cell")[0]
    tables = {}
    data = [p for p in cell.sub("pin") if p.args == "D"][0]
    for timing in data.sub("timing"):
        kind = timing.attr("timing_type").strip('"').split("_")[0]
        for g in timing.groups:
            tables[f"{kind}_{g.name.split('_')[0]}"] = charlib.table(g)[0][0]
    clock = [p for p in cell.sub("pin") if p.args == "CLK"][0]
    (slew,), (clock_slew,) = report["slews"], report["constraint_slews"]
    dr, cr = slew * 0.5 / 0.4, clock_slew * 0.5 / 0.4
    spice = (_ref() / "spice" / f"{LIB}.spice").read_text()
    subckt = re.search(rf"^\.SUBCKT {full} .*?^\.ENDS", spice,
                       re.M | re.S | re.I).group(0)
    wide = 4.0
    low = -(dr + cr) - 0.5

    def window(name, new_high, setup_side):
        def run(work):
            def delay(v):
                s, h = (v, wide) if setup_side else (wide, v)
                return _hand_window_delay(work, subckt, new_high, s, h, cr, dr)
            return name, _hand_bisect(delay, low, wide, wide)
        return run

    def width(name, high, new_high):
        def run(work):
            def delay(w):
                return _hand_width_delay(work, subckt, high, new_high, w, cr, dr)
            return name, _hand_bisect(delay, 0.0, wide, wide)
        return run
    # a rising setup is D arriving high; a rising hold is D leaving low
    tasks = [window("setup_rise", True, True), window("setup_fall", False, True),
             window("hold_rise", False, False), window("hold_fall", True, False)]
    tasks += [width(f"min_pulse_width_{'high' if h else 'low'}", h, n)
              for h in (True, False) for n in (True, False)]

    def run(item):
        k, task = item
        work = tmp_path / f"hand{k}"
        work.mkdir()
        return task(work)
    with ThreadPoolExecutor(len(tasks)) as pool:
        found = list(pool.map(run, enumerate(tasks)))
    bench = {k: v for k, v in found if k.startswith(("setup", "hold"))}
    for name in ("min_pulse_width_high", "min_pulse_width_low"):
        bench[name] = max(v for k, v in found if k == name)
        tables[name] = float(clock.attr(name))
    assert set(bench) == set(tables)
    off = {k: (tables[k], bench[k]) for k in bench
           if abs(tables[k] - bench[k]) > HAND_BAND * abs(bench[k])}
    assert not off, off


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
