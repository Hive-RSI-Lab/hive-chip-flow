"""engine/scripts/qor.py + engine/lib/qorlib.py (#65): the QoR table a
harden records, read back from a real recorded LibreLane run (the PLL
digital block's final/metrics.json, tests/fixtures/qor/pll_digital), and
`qor diff` between two runs."""
from __future__ import annotations

import copy
import json
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "scripts"))
sys.path.insert(0, str(REPO / "engine" / "lib"))

import qor  # noqa: E402
import qorlib  # noqa: E402

RUN = REPO / "tests" / "fixtures" / "qor" / "pll_digital"
TT = "nom_tt_025C_3v30"


def _metrics() -> dict:
    return json.loads((RUN / "final" / "metrics.json").read_text())


def _run(argv, capsys):
    code = qor.main(argv)
    return code, json.loads(capsys.readouterr().out)


def _plant(tmp_path: Path, name: str, edit) -> Path:
    run = tmp_path / name / "harden" / "runs" / "run"
    shutil.copytree(RUN, run)
    m = _metrics()
    edit(m)
    (run / "final" / "metrics.json").write_text(json.dumps(m))
    return run


def test_table_from_recorded_run_matches_known_values():
    rec = qorlib.record(_metrics(), RUN, None)
    m = rec["metrics"]
    assert m["utilization"] == 0.153424
    assert m["stdcell_count"] == 600
    assert m["stdcell_area"] == 7972.97
    assert m["wirelength"] == 4890
    assert m["grt_wirelength"] == 8173
    assert m["setup_ws"] == 2.336559456496964
    assert m["hold_ws"] == 0.5124265601326228
    assert m["power_total"] == 0.0017725294455885887
    assert m["route_drc_errors"] == 0 and m["antenna_nets"] == 0
    assert len(rec["corners"]) == 9
    tt = rec["corners"][TT]
    assert tt["setup_ws"] == 2.7583875964103823
    assert tt["skew_setup"] == 0.675141739402005
    assert tt["hold_ws"] == 1.0947527638725425
    assert rec["missing"] == []
    assert rec["versions"]["librelane"] == "3.1.0.dev3"
    assert rec["versions"]["yosys"] == "0.69"
    assert rec["resolved_sha256"] is not None


def test_missing_metric_is_reported_missing_not_zero(tmp_path, capsys):
    run = _plant(tmp_path, "a", lambda m: (m.pop("route__wirelength"),
                                           m.pop(f"timing__hold__ws__corner:{TT}")))
    code, out = _run(["show", str(run)], capsys)
    assert code == 1
    assert out["qor"]["metrics"]["wirelength"] is None
    assert set(out["qor"]["missing"]) == {"wirelength", f"{TT}.hold_ws"}
    assert {v["kind"] for v in out["violations"]} == {"qor_metric_missing"}
    assert sorted(v["metric"] for v in out["violations"]) == sorted(
        ["wirelength", f"{TT}.hold_ws"])


def test_show_clean_run_passes(capsys):
    code, out = _run(["show", str(RUN)], capsys)
    assert code == 0, out
    assert out["violations"] == []


def test_equal_runs_diff_clean(tmp_path, capsys):
    a = _plant(tmp_path, "a", lambda m: None)
    b = _plant(tmp_path, "b", lambda m: None)
    code, out = _run(["diff", str(b), "--against", str(a)], capsys)
    assert code == 0, out
    assert out["regressions"] == [] and out["lost"] == [] and out["deltas"] == []


def test_planted_worse_wns_and_bigger_area_flagged_and_nothing_else(tmp_path, capsys):
    def worse(m):
        m["timing__setup__ws"] -= 0.3
        m[f"timing__setup__ws__corner:{TT}"] -= 0.3
        m["design__instance__area__stdcell"] *= 1.10
        m["route__wirelength"] += 10        # 0.2%: under the 2% threshold
        m["route__drc_errors"] = 3          # a 0-count that rose
    a = _plant(tmp_path, "a", lambda m: None)
    b = _plant(tmp_path, "b", worse)
    code, out = _run(["diff", str(b), "--against", str(a)], capsys)
    assert code == 1
    flagged = sorted(r["metric"] for r in out["regressions"])
    assert flagged == sorted(["setup_ws", f"{TT}.setup_ws", "stdcell_area",
                              "route_drc_errors"])
    assert {v["kind"] for v in out["violations"]} == {"qor_regression"}
    assert "wirelength" in {d["metric"] for d in out["deltas"]}


def test_better_run_is_not_a_regression(tmp_path, capsys):
    def better(m):
        m["timing__setup__ws"] += 0.5
        m["design__instance__area__stdcell"] *= 0.8
    a = _plant(tmp_path, "a", lambda m: None)
    b = _plant(tmp_path, "b", better)
    code, out = _run(["diff", str(b), "--against", str(a)], capsys)
    assert code == 0, out


def test_threshold_override(tmp_path, capsys):
    a = _plant(tmp_path, "a", lambda m: None)
    b = _plant(tmp_path, "b", lambda m: m.__setitem__(
        "timing__setup__ws", m["timing__setup__ws"] - 0.03))
    code, _ = _run(["diff", str(b), "--against", str(a)], capsys)
    assert code == 0
    code, out = _run(["diff", str(b), "--against", str(a),
                      "--threshold", "ns_abs=0.01"], capsys)
    assert code == 1
    assert [r["metric"] for r in out["regressions"]] == ["setup_ws"]


def test_metric_lost_by_new_run_is_missing_not_a_pass(tmp_path, capsys):
    a = _plant(tmp_path, "a", lambda m: None)
    b = _plant(tmp_path, "b", lambda m: m.pop("power__total"))
    code, out = _run(["diff", str(b), "--against", str(a)], capsys)
    assert code == 1
    assert out["lost"] == ["power_total"]
    assert [v["kind"] for v in out["violations"]] == ["qor_metric_missing"]


def test_diff_against_recorded_harden_at_git_rev(tmp_path, capsys):
    ws = tmp_path / "ws"
    rec_dir = ws / "reports" / "recorded"
    rec_dir.mkdir(parents=True)
    old = qorlib.record(_metrics(), RUN, None)
    (rec_dir / "gate-harden.json").write_text(json.dumps({"qor": old}))
    git = ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run(git[:3] + ["init", "-q"], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "harden"], check=True)
    new = copy.deepcopy(old)
    new["metrics"]["hold_ws"] -= 0.2
    (rec_dir / "gate-harden.json").write_text(json.dumps({"qor": new}))
    code, out = _run(["diff", str(ws), "--against-rev", "HEAD"], capsys)
    assert code == 1
    assert [r["metric"] for r in out["regressions"]] == ["hold_ws"]


def test_version_change_reported_and_not_reproducible():
    rec = qorlib.record(_metrics(), RUN, None)
    env = {"librelane": "3.1.0.dev3", "yosys": "0.70", "openroad": "v2.0-x"}
    rec["versions"]["openroad"] = "v2.0-x"
    why = qorlib.reproducible(rec, env)
    assert why == ["yosys: recorded 0.69, this environment has 0.70"]
    env["yosys"] = "0.69"
    assert qorlib.reproducible(rec, env) == []
    rec2 = copy.deepcopy(rec)
    rec2["versions"]["yosys"] = "0.70"
    d = qorlib.diff(rec, rec2)
    assert d["versions_changed"] == {"yosys": {"old": "0.69", "new": "0.70"}}
    assert d["regressions"] == []


def test_repro_cli_flags_unrecorded_versions(tmp_path, monkeypatch, capsys):
    p = tmp_path / "rec.json"
    p.write_text(json.dumps({"qor": qorlib.extract(_metrics())}))
    monkeypatch.setattr(qorlib, "env_versions", lambda eda: {
        "librelane": "3.1.0.dev3", "yosys": "0.69", "openroad": "v2"})
    code, out = _run(["repro", str(p)], capsys)
    assert code == 1
    assert {v["kind"] for v in out["violations"]} == {"qor_not_reproducible"}


def test_design_doc_shows_the_harden_qor():
    import design_doc
    rec = qorlib.record(_metrics(), RUN, None)
    r = {"gate": "harden", "ran": True, "facts": {"qor": rec}}
    line = design_doc.gate_numbers(r)
    assert "600 cells" in line and "utilisation 15.3" in line and "4890" in line
    ppa = design_doc.ppa_section({"harden": r}, {}, "vde")
    assert "After place and route" in ppa and "routed wirelength 4890" in ppa
    assert "No gate in this flow measures power" not in ppa


def test_recorded_gate_copy_with_facts_qor_loads(tmp_path):
    p = tmp_path / "gate-harden.json"
    rec = qorlib.extract(_metrics())
    p.write_text(json.dumps({"status": "pass", "facts": {"qor": rec}}))
    got, _ = qor.load(str(p))
    assert got["metrics"]["wirelength"] == 4890


def test_metric_the_old_run_lacks_is_not_compared_not_clean(tmp_path, capsys):
    a = _plant(tmp_path, "a", lambda m: m.pop("timing__setup__ws"))
    b = _plant(tmp_path, "b", lambda m: None)
    code, out = _run(["diff", str(b), "--against", str(a)], capsys)
    assert code == 1
    assert out["not_compared"] == ["setup_ws"]
    assert [v["kind"] for v in out["violations"]] == ["qor_metric_missing"]


def test_skew_is_judged_by_magnitude(tmp_path, capsys):
    a = _plant(tmp_path, "a", lambda m: None)
    worse = _plant(tmp_path, "b", lambda m: m.__setitem__(
        "clock__skew__worst_hold", m["clock__skew__worst_hold"] - 0.3))
    better = _plant(tmp_path, "c", lambda m: m.__setitem__(
        "clock__skew__worst_hold", m["clock__skew__worst_hold"] + 0.3))
    code, out = _run(["diff", str(worse), "--against", str(a)], capsys)
    assert code == 1 and [r["metric"] for r in out["regressions"]] == ["skew_hold"]
    code, out = _run(["diff", str(better), "--against", str(a)], capsys)
    assert code == 0, out


def test_repair_buffer_count_is_a_delta_not_a_regression(tmp_path, capsys):
    a = _plant(tmp_path, "a", lambda m: None)
    b = _plant(tmp_path, "b", lambda m: m.__setitem__(
        "design__instance__count__hold_buffer", 1))
    code, out = _run(["diff", str(b), "--against", str(a)], capsys)
    assert code == 0, out
    assert [d["metric"] for d in out["deltas"]] == ["hold_buffer_count"]


def test_gate_recorded_copy_keeps_the_full_qor(tmp_path):
    """qor goes through gate.py's facts into the recorded copy qor reads."""
    import gate
    rec = qorlib.extract(_metrics())
    res = gate.evaluate("harden", {}, {"status": "pass", "violations": [], "qor": rec})
    p = tmp_path / "gate-harden.json"
    p.write_text(json.dumps(res))
    got, _ = qor.load(str(p))
    assert got == rec


def test_msde_top_harden_record_loads_from_the_workspace(tmp_path):
    ws = tmp_path / "ws"
    (ws / "reports" / "recorded").mkdir(parents=True)
    rec = qorlib.extract(_metrics())
    (ws / "reports" / "recorded" / "gate-top_harden.json").write_text(
        json.dumps({"facts": {"qor": rec}}))
    got, where = qor.load(str(ws))
    assert got == rec and where == ws


def test_top_harden_passes_the_qor_through():
    src = (REPO / "engine" / "scripts" / "check_top_harden.py").read_text()
    assert '"run_dir", "metrics", "qor")' in src
