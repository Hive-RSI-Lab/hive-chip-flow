"""engine/lib/stresslib.py and the pex_sim stage on top of it: the DRM
14.1.4.2 allowance, one test per bound, each with the case just past it."""
from __future__ import annotations

import struct
import sys
from pathlib import Path

import numpy as np
import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "scripts"))
sys.path.insert(0, str(REPO / "engine" / "lib"))

import check_pex_sim  # noqa: E402
import stresslib  # noqa: E402
from checklib import CheckError  # noqa: E402

LIMITS = stresslib.load_limits()
DC = 3.63
NS = 1e-9
PERIOD = 80 * NS
WIN = stresslib.Window("cycles", 0.0, 10 * PERIOD, PERIOD)


def pulses(over_v, width, period=PERIOD, count=10, base=3.3, end=None):
    """A flat `base`, with a pulse crossing DC by `over_v` for `width` each
    cycle: a square top on 1 fs edges, so width and peak are exact."""
    t, v, edge = [0.0], [base], 1e-15
    for k in range(count):
        a = (k + 0.25) * period
        t += [a - edge, a, a + width, a + width + edge]
        v += [base, DC + over_v, DC + over_v, base]
    stop = end if end is not None else count * period
    return np.array(t + [stop]), np.array(v + [base])


def one_pulse(over_v, width, at=2e-6, stop=20e-6):
    return pulses(over_v, width, period=at * 4, count=1, end=stop)


def score(pair, windows=()):
    return stresslib.score_gate(*pair, list(windows), LIMITS)


def test_limits_cite_the_section_and_say_they_are_ours():
    ox = LIMITS["gate_oxide"]
    assert "14.1.4.2" in ox["citation"]
    assert "not fitted to any macro" in " ".join(ox["own_numbers"].split())
    assert ox["repeating"] == {"peak_over_v": 0.1, "time_over_share": 0.02,
                              "share_band_v": 0.05}
    assert ox["one_off"] == {"peak_over_v": 0.3, "time_over_s": 1e-6}


def test_under_the_limit_is_clean():
    assert score(pulses(-0.01, 10 * NS), [WIN]) is None


def test_under_the_floor_is_not_an_excursion():
    assert score(pulses(0.0009, 40 * NS), [WIN]) is None
    # just past the floor it is an excursion: inside the share band it
    # passes, but only through the band, and is listed so
    found = score(pulses(0.0011, 40 * NS), [WIN])
    assert found["status"] == "allowance"
    assert found["rows"][0]["band_only"] is True


def test_repeating_inside_every_bound_passes_only_through_the_allowance():
    found = score(pulses(0.08, 1 * NS), [WIN])
    assert found["status"] == "allowance"
    row = found["rows"][0]
    assert row["kind"] == "repeating"
    assert row["share"] == pytest.approx(1 / 80, rel=1e-3)
    assert row["peak_over_v"] == pytest.approx(0.08, abs=1e-9)


def test_repeating_peak_past_the_bound_fails():
    assert score(pulses(0.099, 1 * NS), [WIN])["status"] == "allowance"
    found = score(pulses(0.101, 1 * NS), [WIN])
    assert found["status"] == "fail"
    assert "repeating peak" in found["reasons"][0]


def test_repeating_share_past_the_bound_fails():
    assert score(pulses(0.08, 1.5 * NS), [WIN])["status"] == "allowance"
    found = score(pulses(0.08, 1.7 * NS), [WIN])
    assert found["status"] == "fail"
    assert "of a cycle" in found["reasons"][0]


def test_time_inside_the_band_does_not_count_toward_the_share():
    # 40 mV over for 10% of each cycle: the share counts only time more than
    # 50 mV over, so it passes, and the row says it rests on the band
    found = score(pulses(0.04, 8 * NS), [WIN])
    assert found["status"] == "allowance"
    row = found["rows"][0]
    assert row["share"] == 0
    assert row["share_over_dc"] == pytest.approx(0.1, rel=1e-3)
    assert row["band_only"] is True


def test_time_just_past_the_band_counts():
    found = score(pulses(0.051, 8 * NS), [WIN])
    assert found["status"] == "fail"
    assert "more than 50 mV over the limit" in found["reasons"][0]


def test_the_band_does_not_move_the_peak_bound():
    found = score(pulses(0.101, 0.1 * NS), [WIN])
    assert found["status"] == "fail"
    assert "repeating peak" in found["reasons"][0]


def test_a_pass_inside_the_share_without_the_band_is_not_band_only():
    row = score(pulses(0.08, 1 * NS), [WIN])["rows"][0]
    assert row["band_only"] is False


def test_the_report_lists_band_only_devices_with_peak_and_share():
    found = score(pulses(0.04, 8 * NS), [WIN])
    clean = score(pulses(0.08, 1 * NS), [WIN])
    common = {"class": "gate", "label": "x", "pair": "Vgs", "corner": "tt",
              "deck": "d"}
    rows = [{**common, "device": "XB", **found}, {**common, "device": "XA", **clean}]
    rep = stresslib.summarise(rows, 1, 2, LIMITS, {})
    assert [r["device"] for r in rep["share_band_only"]] == ["XB"]
    listed = rep["share_band_only"][0]
    assert listed["peak_over_v"] == pytest.approx(0.04, abs=1e-9)
    assert listed["share_over_dc"] == pytest.approx(0.1, rel=1e-3)
    assert rep["bounds"]["repeating"]["share_band_v"] == 0.05


def test_share_is_the_worst_single_cycle_not_the_average():
    t, v = pulses(0.08, 1 * NS, count=10)
    extra = 7.25 * PERIOD + 10 * NS
    keep = np.argsort(np.r_[t, extra, extra + 1e-15, extra + 2 * NS, extra + 2 * NS + 1e-15])
    t2 = np.r_[t, extra, extra + 1e-15, extra + 2 * NS, extra + 2 * NS + 1e-15][keep]
    v2 = np.r_[v, 3.3, DC + 0.08, DC + 0.08, 3.3][keep]
    assert score((t2, v2), [WIN])["status"] == "fail"


def test_cycle_average_at_the_limit_fails_with_the_excursion_inside_bounds():
    t = np.array([0, 0.25, 0.25 + 1e-6, 0.26, 0.26 + 1e-6, 1.0]) * PERIOD
    t = np.concatenate([t + k * PERIOD for k in range(10)])
    v = np.tile([3.629, 3.629, 3.7, 3.7, 3.629, 3.629], 10)
    # a mean of 3.63 V with 1% of the cycle over, by raising the base a hair
    found = score((t, v + 0.0009), [WIN])
    assert found["status"] == "fail"
    assert any("cycle average" in r for r in found["reasons"])


def test_one_off_edge_inside_the_bounds():
    found = score(one_pulse(0.25, 0.9e-6))
    assert found["status"] == "allowance"
    assert found["rows"][0]["kind"] == "one_off"
    assert found["rows"][0]["time_over_s"] == pytest.approx(0.9e-6, rel=1e-3)


def test_one_off_peak_past_the_bound_fails():
    assert "one-off peak" in score(one_pulse(0.31, 0.1e-6))["reasons"][0]


def test_one_off_time_past_the_bound_fails():
    assert "bound 1 us" in score(one_pulse(0.05, 1.1e-6))["reasons"][0]


def test_an_excursion_held_to_the_end_of_the_run_fails_however_small():
    t = np.array([0, 1e-6, 1e-6 + 1e-12, 1.2e-6])
    found = score((t, np.array([3.3, 3.3, 3.64, 3.64])))
    assert found["status"] == "fail"
    assert any("when the run ends" in r for r in found["reasons"])


def test_a_repeating_window_does_not_excuse_an_excursion_outside_it():
    win = stresslib.Window("late", 40 * PERIOD, 50 * PERIOD, PERIOD)
    assert "one-off peak" in score(pulses(0.31, 1 * NS), [win])["reasons"][0]


def test_an_excursion_straddling_a_window_answers_to_both_sets():
    t = np.array([0, 2e-7, 2e-7 + 1e-15, 0.3e-6 + 2e-6, 0.3e-6 + 2e-6 + 1e-15, 4e-6])
    v = np.array([3.3, 3.3, 3.68, 3.68, 3.3, 3.3])
    win = stresslib.Window("w", 0.0, 1e-6, 1e-6)
    found = score((t, v), [win])
    assert found["status"] == "fail"
    assert {r["kind"] for r in found["rows"]} == {"repeating", "one_off"}
    assert any("bound 1 us" in r for r in found["reasons"])


def test_junction_is_reported_over_the_reference_and_fails_at_breakdown():
    t = np.array([0.0, 1e-9, 2e-9])
    seen = stresslib.score_junction(t, np.array([3.3, 3.656, 3.3]), 7.0, 3.63)
    assert seen["peak_v"] == pytest.approx(3.656) and not seen["over_breakdown"]
    assert stresslib.score_junction(t, np.array([3.3, 3.6, 3.3]), 7.0, 3.63) is None
    assert stresslib.score_junction(
        t, np.array([3.3, -7.1, 3.3]), 7.0, 3.63)["over_breakdown"]


NETLIST = """* extracted
.subckt top a g vdd vss
X0 a g.t1 vss.t2 vss nfet_03v3 w=1u l=0.28u
+ m=1
X1 a g.t2 vdd.t1 vdd pfet_03v3 w=2u l=0.28u
X2 mid vss cap_nmos_03v3_b c_length=1u c_width=1u
X3 mid a ppolyf_u r_width=1u
.ends
"""
REF = """.subckt top a g vdd vss
XMN_INV a g vss vss nfet_03v3 w=1u l=0.28u
.ends
"""


def parsed():
    devices, others = stresslib.parse_devices(NETLIST, "top")
    stresslib.label_devices(devices, REF, "top")
    stresslib.vectors_of(devices, ["a", "g", "vdd", "vss"], ["out", "in", "vdd", "0"])
    return devices, others


def test_parse_labels_and_vectors():
    devices, others = parsed()
    assert [d["model"] for d in devices] == ["nfet_03v3", "pfet_03v3", "cap_nmos_03v3_b"]
    assert others == {"ppolyf_u": 1}
    assert devices[0]["label"] == "XMN_INV"
    assert devices[1]["label"].startswith("X1 (pfet")
    assert devices[0]["vectors"] == ["out", "xdut.g.t1", "xdut.vss.t2", None]
    assert devices[2]["vectors"] == ["xdut.mid", None]


def test_a_model_with_no_limit_is_a_refusal():
    devices = [{"name": "X9", "model": "nfet_06v0", "nodes": list("dgsb")}]
    with pytest.raises(CheckError, match="no limit"):
        stresslib.check_models(devices, LIMITS, "x")
    with pytest.raises(CheckError, match="no MOS device"):
        stresslib.check_models([], LIMITS, "x")


def test_a_nested_subcircuit_is_a_refusal():
    text = ".subckt top a\nX1 a inner\n.ends\n.subckt inner a\n.ends\n"
    _, others = stresslib.parse_devices(text, "top")
    with pytest.raises(CheckError, match="flat"):
        stresslib.check_flat(text, "top", others)


def write_raw(path: Path, t, waves: dict):
    names = ["time", *waves]
    head = ["Title: t", "Date: d", "Plotname: Transient Analysis", "Flags: real",
            f"No. Variables: {len(names)}", f"No. Points: {len(t)}", "Variables:"]
    head += [f"\t{i}\t{'time' if n == 'time' else f'v({n})'}\t"
             f"{'time' if n == 'time' else 'voltage'}" for i, n in enumerate(names)]
    head.append("Binary:\n")
    rows = np.column_stack([t, *waves.values()])
    path.write_bytes("\n".join(head).encode() + struct.pack(
        f"<{rows.size}d", *rows.ravel()))


def test_binary_raw_rejects_a_truncated_file(tmp_path):
    raw = tmp_path / "a.raw"
    write_raw(raw, np.array([0.0, 1.0]), {"x": np.array([1.0, 2.0])})
    t, store = stresslib.read_binary_raw(raw)
    assert list(t) == [0.0, 1.0] and store["columns"]["x"] == 1
    raw.write_bytes(raw.read_bytes()[:-8])
    with pytest.raises(CheckError, match="data bytes"):
        stresslib.read_binary_raw(raw)


def test_score_run_reads_gate_to_ground_and_every_pair(tmp_path):
    devices, _ = parsed()
    raw = tmp_path / "a.raw"
    t = np.array([0.0, 1e-7, 1e-7 + 1e-15, 2e-7, 2e-7 + 1e-15, 1e-6])
    bump = np.array([0, 0, 0.7, 0.7, 0, 0])
    waves = {v: np.full(6, 0.0) for d in devices for v in d["vectors"] if v}
    waves["xdut.g.t1"] = 3.3 + bump
    write_raw(raw, t, waves)
    tt, store = stresslib.read_binary_raw(raw)
    rows = stresslib.score_run(tt, store, devices, [], LIMITS)
    gate = {(r["device"], r["pair"]): r for r in rows if r["class"] == "gate"}
    assert ("X0", "Vgb") in gate
    assert gate[("X0", "Vgb")]["status"] == "fail"       # 370 mV over, past the one-off peak


def test_score_run_stopping_before_its_window_ends_is_a_refusal():
    devices, _ = parsed()
    data = np.zeros((2, 6))
    store = {"data": data, "columns": {}}
    with pytest.raises(CheckError, match="before its"):
        stresslib.score_run(np.array([0.0, 1e-9]), store, devices,
                            [stresslib.Window("w", 0, 1e-6, 1e-6)], LIMITS)


def _sidecar(tmp_path, **deck):
    (tmp_path / "b.cir").write_text("x\n")
    body = {"name": "d", "bench": "b.cir", "corners": ["tt"], **deck}
    side = tmp_path / "top_stress.yaml"
    import yaml
    side.write_text(yaml.safe_dump({"decks": [body]}))
    return side


def test_sidecar_refuses_a_window_of_part_cycles(tmp_path):
    side = _sidecar(tmp_path, repeating=[
        {"name": "w", "start_s": 0.0, "stop_s": 2.5e-7, "period_s": 1e-7}])
    with pytest.raises(CheckError, match="whole number"):
        stresslib.load_sidecar(side)


def test_sidecar_refuses_a_bench_outside_its_folder_and_an_empty_list(tmp_path):
    with pytest.raises(CheckError, match="beside"):
        stresslib.load_sidecar(_sidecar(tmp_path, bench="../b.cir"))
    side = tmp_path / "e.yaml"
    side.write_text("decks: []\n")
    with pytest.raises(CheckError, match="non-empty"):
        stresslib.load_sidecar(side)


def test_with_control_writes_binary_and_refuses_a_control_block():
    bench = "t\n.tran 1n 1u\n.end\n"
    out = stresslib.with_control(bench, "r.raw", ["a", "b"], "b")
    assert "set filetype=binary" in out and "save v(a) v(b)" in out
    assert out.rstrip().endswith(".end") and out.index(".control") < out.rindex(".end")
    with pytest.raises(CheckError, match="control"):
        stresslib.with_control(bench + ".control\n.endc\n", "r", ["a"], "b")
    with pytest.raises(CheckError, match="tran"):
        stresslib.with_control("t\n.end\n", "r", ["a"], "b")


def test_summary_lists_allowance_devices_and_never_a_failing_one():
    common = {"model": "nfet_03v3", "class": "gate", "corner": "c", "deck": "d"}
    row = {"kind": "one_off", "peak_v": 3.7, "peak_over_v": 0.07, "t_peak_s": 1e-6,
           "time_over_s": 1e-7, "events": 1}
    rows = [
        {**common, "device": "X1", "label": "A", "pair": "Vgs",
         "status": "allowance", "rows": [row], "reasons": []},
        {**common, "device": "X2", "label": "B", "pair": "Vgs",
         "status": "allowance", "rows": [row], "reasons": []},
        {**common, "device": "X2", "label": "B", "pair": "Vgd", "corner": "e",
         "status": "fail", "rows": [row], "reasons": ["x"]},
    ]
    out = stresslib.summarise(rows, 1, 2, LIMITS, {})
    assert [a["device"] for a in out["allowance_only"]] == ["X1"]
    assert out["allowance_only"][0]["peak_v"] == 3.7
    assert out["devices_failing"] == ["X2"]
    assert "not fitted" in " ".join(out["allowance_is_ours"].split())


BENCH = """* stress {corner} {temp_c} {extracted}
xdut out in vdd 0 top
.tran 1n 1u
.end
"""
TOP = """.subckt top a g vdd vss
X0 a g vss vss nfet_03v3 w=1u l=0.28u
.ends
"""


def stage(tmp_path, monkeypatch, wave, kind_of_run=None):
    ws = tmp_path / "ws"
    (ws / "layout_ref").mkdir(parents=True)
    work = ws / "log" / "pex_sim"
    work.mkdir(parents=True)
    extracted = work / "top.spice"
    extracted.write_text(TOP)
    (ws / "layout_ref" / "b.cir").write_text(BENCH)
    side = ws / "layout_ref" / "top_stress.yaml"
    side.write_text("decks:\n- {name: d, bench: b.cir, corners: [tt]}\n")
    corner = {"name": "tt", "process": "typical", "temp_c": 27}
    monkeypatch.setattr(check_pex_sim.sim_run, "spec_sweep",
                        lambda _ws: ([corner], []))
    monkeypatch.setattr(check_pex_sim, "_named",
                        lambda sweep, name: corner if name == "tt" else None)
    monkeypatch.setattr(check_pex_sim, "corner_tokens", lambda c, v: {
        "corner": "typical", "res_corner": "r", "mim_corner": "m",
        "temp_c": 27, "vdd": "3.3"})

    def fake(bench, cwd, timeout):
        assert "set filetype=binary" in Path(bench).read_text()
        t = np.array([0.0, 1e-7, 1e-7 + 1e-15, 2e-7, 2e-7 + 1e-15, 1e-6])
        write_raw(Path(cwd) / "d__tt.raw", t, {
            "out": np.zeros(6), "in": wave * np.array([0, 0, 1, 1, 0, 0]) + 3.3,
            "vdd": np.full(6, 3.3)})
        return ""

    monkeypatch.setattr(check_pex_sim.layoutlib, "run_ngspice", fake)
    violations: list = []
    facts = check_pex_sim.stress_stage(
        ws, side, "top", work, extracted, TOP, "top", "top", "layout/top.gds",
        "/pdk", 3.3, 60, violations)
    return facts, violations, work


def test_stage_records_an_allowance_pass_and_removes_the_raw(tmp_path, monkeypatch):
    facts, violations, work = stage(tmp_path, monkeypatch, 0.4)
    assert violations == []
    assert {a["pair"] for a in facts["allowance_only"]} >= {"Vgb", "Vgs"}
    assert not list((work / "stress").glob("*.raw"))


def test_stage_turns_a_breach_into_a_named_violation(tmp_path, monkeypatch):
    _, violations, _ = stage(tmp_path, monkeypatch, 0.8)
    assert {v["kind"] for v in violations} == {"device_overstress"}
    assert all("tt / d" in v["msg"] for v in violations)


REAL_BENCH = """* real ngspice, one inverter driven 50 mV over the limit
.include '{pdk}/libs.tech/ngspice/design.spice'
.lib '{pdk}/libs.tech/ngspice/sm141064.spice' {corner}
.include '{extracted}'
.temp {temp_c}
vvdd vdd 0 3.3
vin in 0 pulse(0 3.72 10n 1n 1n 50n 200n)
xdut out in vdd 0 top
.tran 1n 1u
.end
"""
REAL_TOP = """.subckt top a g vdd vss
X0 a g vss vss nfet_03v3 w=1u l=0.28u
X1 a g vdd vdd pfet_03v3 w=2u l=0.28u
.ends
"""


@pytest.mark.slow
def test_stage_reads_a_real_ngspice_raw(tmp_path, monkeypatch):
    """The raw-file names and format are ngspice's: only the tool says."""
    ws = tmp_path / "ws"
    (ws / "layout_ref").mkdir(parents=True)
    work = ws / "log" / "pex_sim"
    work.mkdir(parents=True)
    extracted = work / "top.spice"
    extracted.write_text(REAL_TOP)
    (ws / "layout_ref" / "b.cir").write_text(REAL_BENCH)
    side = ws / "layout_ref" / "top_stress.yaml"
    side.write_text("decks:\n- {name: d, bench: b.cir, corners: [tt], repeating: "
                    "[{name: w, start_s: 2.0e-7, stop_s: 1.0e-6, period_s: 2.0e-7}]}\n")
    corner = {"name": "tt", "process": "typical", "temp_c": 27}
    monkeypatch.setattr(check_pex_sim.sim_run, "spec_sweep",
                        lambda _ws: ([corner], []))
    monkeypatch.setattr(check_pex_sim, "_named", lambda sweep, name: corner)
    monkeypatch.setattr(check_pex_sim, "corner_tokens", lambda c, v: {
        "corner": "typical", "res_corner": "r", "mim_corner": "m",
        "temp_c": 27, "vdd": "3.3"})
    violations: list = []
    facts = check_pex_sim.stress_stage(
        ws, side, "top", work, extracted, REAL_TOP, "top", "top", "layout/top.gds",
        check_pex_sim.layoutlib.pdk_root(), 3.3, 120, violations)
    # the input sits at 3.72 V for 25% of the cycle: over on share, in every
    # gate pair to a rail at 0 V
    assert "X0" in facts["devices_failing"], facts
    assert any("of a cycle" in v["msg"] for v in violations), violations


def test_exponent_read_as_string_is_a_number():
    # PyYAML gives "8e-08" back as a string; the sidecar must still load.
    assert stresslib._number("8e-08", "w period_s") == pytest.approx(8e-08)
    with pytest.raises(CheckError):
        stresslib._number("eighty", "w period_s")
