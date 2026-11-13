"""The boundary gate and interface version 2 (issue #110). The fast half is the
load shapes, the level table, the judging and the declaration. The slow half is
real ngspice on an ideal driver with a known answer, and on inverter macros
with the faults the gate exists to catch."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "scripts"))
sys.path.insert(0, str(REPO / "engine" / "lib"))

import attest  # noqa: E402
import boundary  # noqa: E402
import check_boundary  # noqa: E402
import layoutlib  # noqa: E402
import macrolib  # noqa: E402

TT = "tt_025C_3v30"
SS = "ss_125C_3v00"
FF = "ff_n40C_3v60"
CELL = "gf180mcu_fd_sc_mcu7t5v0__dffq_1"
LEVEL = boundary.load_levels()["cmos_3v3"]
PINS = {"a": "d2a", "y": "a2d"}
GOOD = {"static": {},
        "a2d": {"y": {"from": "a", "window_ns": 200}},
        "d2a": {"a": {"observe": "y"}}}


# ---- load shapes

@pytest.mark.parametrize("load", [
    {"cap_ff": 180}, {"cap_ff": 0.5, "note": "two gates"},
    {"pin": f"{CELL}/CLK"}, {"pin": f"{CELL}/CLK", "count": 4}])
def test_typed_loads_are_accepted(load):
    assert boundary.load_problem(load) is None


@pytest.mark.parametrize("load, words", [
    ("2pF", "not typed"), (None, "not typed"),
    ({}, "exactly one"), ({"cap_ff": 1, "pin": f"{CELL}/CLK"}, "exactly one"),
    ({"cap_ff": 0}, "positive"), ({"cap_ff": True}, "positive"),
    ({"cap_ff": "20"}, "positive"), ({"pin": "dffq_1"}, "<liberty cell>/<pin>"),
    ({"pin": "/CLK"}, "<liberty cell>/<pin>"),
    ({"cap_ff": 1, "count": 0}, "whole number"),
    ({"cap_ff": 1, "count": 1.5}, "whole number"),
    ({"cap_ff": 1, "fanout": 3}, "unknown fields")])
def test_untyped_loads_are_named(load, words):
    assert words in boundary.load_problem(load)


def test_a_note_does_not_make_two_loads_differ():
    assert (boundary.comparable_load({"cap_ff": 5, "note": "x"})
            == boundary.comparable_load({"cap_ff": 5}))


LIBERTY = """\
  cell (other) {
    pin (CLK) { capacitance : 9.0; max_transition : 1.0; }
  }


  cell (c1) {
    pin (D) { capacitance : 0.002; max_transition : 8.0; }


    pin(CLK) {
      capacitance : 0.004;
      max_transition : 7.5;
    }
    pin (Q) { direction : output; max_capacitance : 0.3; }
  }
"""


def test_load_resolves_against_the_named_cell_and_pin():
    got = boundary.resolve_load({"pin": "c1/CLK", "count": 3}, LIBERTY)
    assert got["pf"] == pytest.approx(0.012)
    assert got["max_transition_ns"] == 7.5


def test_plain_capacitance_sets_no_pin_limit():
    assert boundary.resolve_load({"cap_ff": 180, "count": 2}, LIBERTY) == {
        "pf": pytest.approx(0.36), "max_transition_ns": None}


@pytest.mark.parametrize("pin, words", [
    ("nope/CLK", "no cell"), ("c1/NOPE", "no pin"),
    ("c1/Q", "no capacitance")])
def test_a_load_the_liberty_cannot_resolve_is_refused(pin, words):
    with pytest.raises(boundary.Refused, match=words):
        boundary.resolve_load({"pin": pin}, LIBERTY)


# ---- the level table against what it stands for

@pytest.mark.parametrize("corner", macrolib.STA_CORNERS)
def test_level_supply_is_the_corners_supply(corner):
    assert LEVEL["corners"][corner]["vdd"] == macrolib.corner_of(corner)["vdd"]


def test_level_table_covers_exactly_the_sta_corners():
    assert set(LEVEL["corners"]) == set(macrolib.STA_CORNERS)


@pytest.mark.slow
@pytest.mark.parametrize("corner", macrolib.STA_CORNERS)
def test_level_table_is_the_liberty(corner):
    import re
    text = boundary.library_text(layoutlib.pdk_root(), corner)
    head = text[:text.index("cell (") if "cell (" in text else 20000]

    def number(name):
        return float(re.search(rf"{name}\s*:\s*([0-9.]+)", head).group(1))

    assert number("slew_lower_threshold_pct_rise") / 100 == LEVEL["slew_lower"]
    assert number("slew_upper_threshold_pct_rise") / 100 == LEVEL["slew_upper"]
    assert number("slew_derate_from_library") == LEVEL["slew_derate"]
    limit = boundary.liberty_pin(text, CELL, "CLK")["max_transition_ns"]
    assert limit == LEVEL["corners"][corner]["max_transition_ns"]


# ---- the arithmetic

def test_table_transition_is_the_liberty_unit():
    # 10-90% is 0.8 of the swing, 30-70% is 0.4 of it, derated by 0.5: the
    # two conversions cancel for this library
    assert boundary.table_transition(4.0, LEVEL) == pytest.approx(4.0)
    other = {**LEVEL, "slew_derate": 1.0}
    assert boundary.table_transition(4.0, other) == pytest.approx(2.0)


def test_limit_is_the_tighter_of_library_and_pin():
    assert boundary.transition_limit(LEVEL, TT, None) == 8.6
    assert boundary.transition_limit(LEVEL, TT, 2.0) == 2.0
    assert boundary.transition_limit(LEVEL, TT, 20.0) == 8.6


def _measured(high=3.3, low=0.0, rise=1.0, fall=1.0):
    return {"v_high": high, "v_low": low, "load_pf": 0.1,
            "rise_transition_ns": rise, "fall_transition_ns": fall}


def _kinds(found):
    return [kind for kind, _ in found]


def test_a_clean_output_has_no_findings():
    assert boundary.judge_output("y", TT, LEVEL, _measured(), 8.6) == []


@pytest.mark.parametrize("high, low, expected", [
    (2.30, 0.0, ["boundary_swing"]),           # just under 0.7 * 3.3
    (3.3, 1.0, ["boundary_swing"]),            # just over 0.3 * 3.3
    (2.0, 1.5, ["boundary_swing", "boundary_swing"])])
def test_swing_is_held_to_vih_and_vil(high, low, expected):
    assert _kinds(boundary.judge_output(
        "y", TT, LEVEL, _measured(high=high, low=low), 8.6)) == expected


def test_swing_at_exactly_vih_and_vil_passes():
    assert boundary.judge_output(
        "y", TT, LEVEL, _measured(high=0.7 * 3.3, low=0.3 * 3.3), 8.6) == []


def test_swing_is_judged_against_the_corners_own_supply():
    # 2.4 V clears vih at ss (2.1 V) and not at ff (2.52 V)
    assert boundary.judge_output("y", SS, LEVEL, _measured(high=2.4), 15.6) == []
    assert _kinds(boundary.judge_output(
        "y", FF, LEVEL, _measured(high=2.4), 5.2)) == ["boundary_swing"]


@pytest.mark.parametrize("edge", ["rise", "fall"])
def test_slow_edge_fails_on_either_direction(edge):
    measured = _measured(**{edge: 8.7})
    assert _kinds(boundary.judge_output("y", TT, LEVEL, measured, 8.6)) == [
        "boundary_transition"]


def test_missing_edge_is_a_finding_not_a_pass():
    found = boundary.judge_output("y", TT, LEVEL, _measured(rise=None), 8.6)
    assert _kinds(found) == ["boundary_transition"]
    assert "no complete" in found[0][1]


@pytest.mark.parametrize("threshold, expected", [
    (1.65, []), (1.0, []), (2.3, []),
    (0.98, ["boundary_threshold"]), (2.32, ["boundary_threshold"]),
    (None, ["boundary_threshold"])])
def test_input_switching_point_is_held_inside_the_band(threshold, expected):
    assert _kinds(boundary.judge_input("a", TT, LEVEL, threshold)) == expected


# ---- the declaration

def test_a_complete_section_parses():
    decl = boundary.parse_section(GOOD, PINS)
    assert decl.outputs["y"].frm == "a"
    assert decl.inputs["a"].observe == "y" and decl.inputs["a"].rising


@pytest.mark.parametrize("mutate, words", [
    (lambda s: s["a2d"].pop("y"), "exactly the a2d"),
    (lambda s: s["d2a"].update({"extra": {"observe": "y"}}), "exactly the d2a"),
    (lambda s: s["d2a"]["a"].pop("observe"), "no observe"),
    (lambda s: s["a2d"]["y"].pop("window_ns"), "no window_ns"),
    (lambda s: s["a2d"]["y"].pop("from"), "exactly one of source"),
    (lambda s: s["a2d"]["y"].update({"source": "a"}), "exactly one of source"),
    (lambda s: s["a2d"]["y"].update({"window_ns": 0}), "positive"),
    (lambda s: s["d2a"]["a"].update({"ramp": "sideways"}), "up or down"),
    (lambda s: s["d2a"]["a"].update({"vih": 1.0}), "unknown fields"),
    (lambda s: s.update({"waive": True}), "unknown fields")])
def test_an_incomplete_section_is_refused(mutate, words):
    section = json.loads(json.dumps(GOOD))
    mutate(section)
    with pytest.raises(boundary.Refused, match=words):
        boundary.parse_section(section, PINS)


# ---- registration

def test_only_the_pll_may_stay_on_version_1():
    assert set(boundary.V1_BLOCKS) == {"pll"}


@pytest.mark.parametrize("path, expected", [
    ("blocks/pll", True), ("blocks/r2r_dac", False), ("blocks/other", False),
    ("work/pll", False)])
def test_version_1_exemption_is_by_released_name(tmp_path, path, expected):
    assert boundary.is_v1_block(tmp_path / path) is expected


def _untyped_ws(tmp_path, name) -> Path:
    ws = tmp_path / "blocks" / name
    ws.mkdir(parents=True)
    (ws / "interface.yaml").write_text(yaml.safe_dump({"version": 1, "signals": [
        {"name": "a", "direction": "d2a", "level": "cmos_3v3", "width": 1,
         "load": "1pF"}]}))
    return ws


def test_a_v1_block_does_not_owe_the_gate_at_release(tmp_path):
    owed = attest.owed_gates(_untyped_ws(tmp_path, "pll"), "msde")
    assert "boundary" not in owed
    assert "cosim" in owed


def test_any_other_block_owes_the_gate_at_release(tmp_path):
    assert "boundary" in attest.owed_gates(_untyped_ws(tmp_path, "sensor"), "msde")


def test_a_macro_with_no_crossing_pin_does_not_owe_it(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "interface.yaml").write_text("version: 2\nsignals: []\n")
    assert "no crossing pin" in check_boundary.not_applicable_at_release(ws)


# ---- the workspace, up to the first simulation

IDEAL = """\
.subckt invm a y vdd vss
bq n1 vss v = v(a) > {threshold} * v(vdd) ? v(vdd) * {high} : v(vss)
rq n1 y {r}
.ends invm
"""
INVERTER = """\
.subckt invm a y vdd vss
XP y a vdd vdd pfet_03v3 w={wp}u l=0.28u
XN y a vss vss nfet_03v3 w={wn}u l=0.28u
.ends invm
"""


def ideal(threshold=0.5, high=1.0, r=1000):
    """A driver with a known answer: y = (a > threshold) through r ohms."""
    return IDEAL.format(threshold=threshold, high=high, r=r)


def make_ws(tmp_path, netlist, section=GOOD, load=None, version=2) -> Path:
    ws = tmp_path / "ws"
    (ws / "analog" / "spec").mkdir(parents=True)
    (ws / "analog" / "netlist").mkdir()
    (ws / "digital" / "rtl").mkdir(parents=True)
    (ws / "analog" / "state.json").write_text(json.dumps({"block": "invm"}))
    (ws / "analog" / "netlist" / "invm.cir").write_text(netlist)
    (ws / "analog" / "spec" / "spec.yaml").write_text(yaml.safe_dump(
        {"top": "invm", **({"boundary": section} if section else {})}))
    (ws / "interface.yaml").write_text(yaml.safe_dump({"version": version, "signals": [
        {"name": "a", "direction": "d2a", "level": "cmos_3v3", "width": 1,
         "load": {"cap_ff": 10}},
        {"name": "y", "direction": "a2d", "level": "cmos_3v3", "width": 1,
         "load": load or {"cap_ff": 1000}}]}))
    return ws


def run(ws):
    payload, _ = check_boundary.run(["--workspace", str(ws)])
    return payload


def kinds(payload):
    return {v["kind"] for v in payload["violations"]}


def test_a_macro_with_crossing_pins_and_no_section_fails(tmp_path):
    payload = run(make_ws(tmp_path, ideal(), section=None))
    assert payload["status"] == "violations"
    assert kinds(payload) == {"boundary_undeclared"}


def test_an_untyped_interface_is_refused(tmp_path):
    with pytest.raises(boundary.Refused, match="version"):
        run(make_ws(tmp_path, ideal(), version=1))


def test_an_incomplete_section_is_refused_before_any_simulation(tmp_path):
    section = {"a2d": {"y": {"from": "a", "window_ns": 200}}}
    with pytest.raises(boundary.Refused, match="exactly the d2a"):
        run(make_ws(tmp_path, ideal(), section=section))


# ---- real ngspice

def _at(payload, corner, pin):
    return payload["pins"][corner][pin]


@pytest.mark.slow
def test_known_answer_ideal_driver(tmp_path):
    """1 kohm into 1 pF: rails exactly, a 10-90% edge of RC ln 9 = 2.197 ns,
    and a switching point at the driver's own vdd/2, at every corner."""
    import math
    payload = run(make_ws(tmp_path, ideal()))
    assert payload["status"] == "pass", payload["violations"]
    for corner in macrolib.STA_CORNERS:
        vdd = macrolib.corner_of(corner)["vdd"]
        out = _at(payload, corner, "y")
        assert out["v_high"] == pytest.approx(vdd, abs=0.01)
        assert out["v_low"] == pytest.approx(0.0, abs=0.01)
        expected = 1000 * 1e-12 * math.log(9) * 1e9
        assert out["rise_ns"] == pytest.approx(expected, rel=0.05)
        assert out["fall_ns"] == pytest.approx(expected, rel=0.05)
        assert _at(payload, corner, "a")["threshold_v"] == pytest.approx(vdd / 2, abs=0.02)


@pytest.mark.slow
def test_a_real_inverter_at_3v3_swings_rail_to_rail(tmp_path):
    payload = run(make_ws(tmp_path, INVERTER.format(wn=4, wp=8)))
    assert payload["status"] == "pass", payload["violations"]
    out = _at(payload, TT, "y")
    assert out["v_high"] == pytest.approx(3.3, abs=0.02)
    assert out["v_low"] == pytest.approx(0.0, abs=0.02)
    assert 0.2 < out["rise_ns"] < 5.0


@pytest.mark.slow
def test_output_stage_on_a_2v_rail_fails_swing(tmp_path):
    payload = run(make_ws(tmp_path, ideal(high=2.0 / 3.3)))
    assert "boundary_swing" in kinds(payload)
    assert any(v["kind"] == "boundary_swing" and TT in v["refs"]
               for v in payload["violations"])


@pytest.mark.slow
def test_w_over_8_driver_into_a_heavy_load_fails_transition(tmp_path):
    netlist = INVERTER.format(wn=4, wp=8)
    section = {"static": {}, "a2d": {"y": {"from": "a", "window_ns": 800, "step_ns": 0.05}},
               "d2a": {"a": {"observe": "y"}}}
    payload = run(make_ws(tmp_path, netlist, section=section, load={"cap_ff": 4000}))
    assert kinds(payload) == {"boundary_transition"}
    assert payload["status"] == "violations"


@pytest.mark.slow
def test_full_size_driver_into_the_same_load_passes(tmp_path):
    # the control for the fault above, eight times its width: the load alone
    # is not what fails it
    section = {"static": {}, "a2d": {"y": {"from": "a", "window_ns": 800, "step_ns": 0.05}},
               "d2a": {"a": {"observe": "y"}}}
    payload = run(make_ws(tmp_path, INVERTER.format(wn=32, wp=64), section=section,
                          load={"cap_ff": 4000}))
    assert payload["status"] == "pass", payload["violations"]


@pytest.mark.slow
def test_input_switching_outside_the_band_fails_threshold(tmp_path):
    payload = run(make_ws(tmp_path, ideal(threshold=0.2)))
    assert kinds(payload) == {"boundary_threshold"}


@pytest.mark.slow
def test_load_named_by_pin_uses_that_corners_capacitance(tmp_path):
    payload = run(make_ws(tmp_path, ideal(), load={"pin": f"{CELL}/CLK"}))
    assert payload["status"] == "pass", payload["violations"]
    pdk = layoutlib.pdk_root()
    for corner in macrolib.STA_CORNERS:
        want = boundary.liberty_pin(boundary.library_text(pdk, corner), CELL, "CLK")
        out = _at(payload, corner, "y")
        assert out["load_pf"] == want["capacitance_pf"]
        assert out["max_transition_ns"] == want["max_transition_ns"]
