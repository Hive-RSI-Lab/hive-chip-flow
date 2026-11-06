"""check_harden.py's signoff metrics (#71), on a run recorded from the PLL's
digital harden: final/metrics.json trimmed to the keys read here, the repair
step's log naming the floating nets, and the last check-antennas report."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import check_harden  # noqa: E402
import speclib  # noqa: E402
from test_check_harden import _fake_run_factory, make_ws  # noqa: E402

RECORDED = REPO / "tests" / "fixtures" / "harden_signoff" / "pll_digital"
LOG = "32-openroad-repairdesignpostgpl/openroad-repairdesignpostgpl.log"
RPT = "48-openroad-checkantennas-1/reports/antenna.rpt"
TOP = "tt_um_x"


@pytest.fixture
def run_dir(tmp_path):
    d = tmp_path / "run"
    shutil.copytree(RECORDED, d)
    return d


def recorded() -> dict:
    return json.loads((RECORDED / "final" / "metrics.json").read_text("utf-8"))


def judge(run_dir, signoff=None, **changes):
    metrics = {**recorded(), **changes}
    table, violations = check_harden.signoff_metrics(
        metrics, signoff or {}, run_dir, TOP)
    return table, violations


def kinds(violations):
    return sorted(v["kind"] for v in violations)


def test_recorded_clean_run_passes(run_dir):
    table, violations = judge(run_dir)
    assert violations == []
    assert all(row["status"] == "ok" for row in table.values())
    # its 2 floating nets are the power nets, named in the step's log
    assert table["timing__drv__floating__nets"]["value"] == 2
    assert table["timing__drv__floating__nets"]["power_nets"] == ["VGND", "VPWR"]


@pytest.mark.parametrize("metric, value, kind", [
    ("antenna__violating__nets", 3, "antenna_violation"),
    ("route__antenna_violation__count", 1, "antenna_violation"),
    ("timing__unannotated_net_filtered__count", 4, "unannotated_net"),
    ("design__power_grid_violation__count", 1, "pdn_violation"),
    ("ir__drop__worst", 0.2, "ir_drop_over_budget"),
])
def test_planted_metric_over_its_limit_is_refused(run_dir, metric, value, kind):
    table, violations = judge(run_dir, **{metric: value})
    assert kinds(violations) == [kind]
    assert violations[0]["severity"] == "error"
    assert violations[0]["metric"] == metric
    assert table[metric]["status"] == "over"


@pytest.mark.parametrize("metric", [
    "antenna__violating__nets", "route__antenna_violation__count",
    "timing__drv__floating__nets", "timing__unannotated_net_filtered__count",
    "design__power_grid_violation__count", "ir__drop__worst",
    "ir__voltage__worst",
])
def test_missing_metric_is_reported_missing_not_zero(run_dir, metric):
    metrics = recorded()
    del metrics[metric]
    table, violations = check_harden.signoff_metrics(metrics, {}, run_dir, TOP)
    assert kinds(violations) == ["signoff_metric_missing"]
    assert violations[0]["metric"] == metric
    row = "ir__drop__worst" if metric == "ir__voltage__worst" else metric
    assert table[row]["status"] == "missing"


@pytest.mark.parametrize("bad", [None, "0", True, float("nan"), float("inf")])
def test_a_value_that_is_no_number_is_missing(run_dir, bad):
    _table, violations = judge(run_dir, antenna__violating__nets=bad)
    assert kinds(violations) == ["signoff_metric_missing"]


def test_ir_budget_is_a_share_of_the_rail_and_the_spec_moves_it(run_dir):
    drop = 3.3 * 0.06 / 0.94  # 6% of the rail, worst + drop
    over = judge(run_dir, ir__drop__worst=drop)
    assert kinds(over[1]) == ["ir_drop_over_budget"]
    assert over[0]["ir__drop__worst"]["limit"] == pytest.approx(0.05 * 3.51, abs=1e-3)
    roomy = judge(run_dir, {"ir_budget_pct": 10}, ir__drop__worst=drop)
    assert roomy[1] == []
    tight = judge(run_dir, {"ir_budget_pct": 0.001})
    assert kinds(tight[1]) == ["ir_drop_over_budget"]


def _name_floating(run_dir, *names):
    (run_dir / LOG).write_text(
        f"[WARNING RSZ-0020] found {len(names)} floating nets.\n"
        + "".join(f" {n}\n" for n in names) + "+ next\n", encoding="utf-8")


def test_a_real_floating_net_is_named_and_refused(run_dir):
    _name_floating(run_dir, "VGND", "VPWR", "lost_wire")
    table, violations = judge(run_dir, timing__drv__floating__nets=3)
    assert kinds(violations) == ["floating_net"]
    assert violations[0]["refs"] == ["lost_wire"]
    assert "VPWR" not in violations[0]["msg"]
    assert table["timing__drv__floating__nets"]["status"] == "over"


def test_a_waiver_must_name_the_net(run_dir):
    _name_floating(run_dir, "VGND", "VPWR", "lost_wire")
    waived = {"floating_nets_waived": {"lost_wire": "spare output, bond-out only"}}
    table, violations = judge(run_dir, waived, timing__drv__floating__nets=3)
    row = table["timing__drv__floating__nets"]
    assert violations == [] and row["waived"] == ["lost_wire"]
    other = {"floating_nets_waived": {"other_wire": "not this net"}}
    table, violations = judge(run_dir, other, timing__drv__floating__nets=3)
    assert kinds(violations) == ["floating_net"]
    assert table["timing__drv__floating__nets"]["unused_waivers"] == ["other_wire"]


def test_a_count_no_log_names_is_refused_even_with_a_waiver(run_dir):
    (run_dir / LOG).unlink()
    waived = {"floating_nets_waived": {"VGND": "x", "VPWR": "y"}}
    _table, violations = judge(run_dir, waived)
    assert kinds(violations) == ["floating_net"]
    assert "no step log" in violations[0]["msg"]


def test_a_log_that_names_too_few_is_refused(run_dir):
    (run_dir / LOG).write_text(
        "[WARNING RSZ-0020] found 2 floating nets.\n VPWR\n+ next\n",
        encoding="utf-8")
    assert kinds(judge(run_dir)[1]) == ["floating_net"]


def test_antenna_finding_names_the_nets_of_the_latest_report(run_dir):
    (run_dir / RPT).write_text(
        "Net: gate_net\n  Pin: u1/A\nNet: other[3]\n", encoding="utf-8")
    _table, violations = judge(run_dir, antenna__violating__nets=2)
    assert violations[0]["refs"] == ["gate_net", "other[3]"]
    assert "gate_net" in violations[0]["msg"]
    assert violations[0]["report"] == RPT


@pytest.mark.parametrize("value", [
    {"ir_budget_pct": 0}, {"ir_budget_pct": 101}, {"ir_budget_pct": True},
    {"ir_budget_pct": "5"}, {"floating_nets_waived": ["x"]},
    {"floating_nets_waived": {"x": ""}}, {"typo": 1}, "nope",
])
def test_malformed_harden_signoff_is_refused(tmp_path, monkeypatch, capsys, value):
    assert speclib.harden_signoff({"harden_signoff": value}) is None
    assert any(f["kind"] == "bad_harden_signoff" for f in speclib.lint_spec(
        {"harden_signoff": value}))
    ws = make_ws(tmp_path)
    spec_path = ws / "spec" / "spec.yaml"
    spec = speclib.load_spec(spec_path)
    spec["harden_signoff"] = value
    spec_path.write_text(json.dumps(spec), encoding="utf-8")

    def never(*_a, **_k):
        raise AssertionError("refused too late: the toolchain was called")

    monkeypatch.setattr(check_harden.subprocess, "run", never)
    code = check_harden.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2, out
    assert "harden_signoff" in out["remediation"]


def _flow(metrics):
    """A librelane stand-in whose run finishes with these metrics."""
    def behavior(cwd):
        run_dir = cwd / "runs" / "run"
        shutil.copytree(RECORDED, run_dir, dirs_exist_ok=True)
        final = run_dir / "final"
        for fmt in check_harden.EXPECTED_FORMATS:
            if fmt != "metrics.json":
                (final / fmt).mkdir(parents=True, exist_ok=True)
                (final / fmt / f"x.{fmt}").write_text("x", encoding="utf-8")
        (final / "metrics.json").write_text(json.dumps(metrics), encoding="utf-8")
        (final / "odb").mkdir(parents=True, exist_ok=True)
        (final / "odb" / "tt_um_counter8.odb").write_text("x", encoding="utf-8")
        (run_dir / "flow.log").write_text("Flow complete.\n", encoding="utf-8")
        return subprocess.CompletedProcess(["eda"], 0, stdout="", stderr="")
    return behavior


def _gate(tmp_path, monkeypatch, capsys, metrics):
    ws = make_ws(tmp_path)
    monkeypatch.setattr(check_harden.subprocess, "run",
                        _fake_run_factory(_flow(metrics)))
    code = check_harden.main(["--workspace", str(ws)])
    return code, json.loads(capsys.readouterr().out)


def test_gate_passes_a_recorded_clean_run(tmp_path, monkeypatch, capsys):
    code, out = _gate(tmp_path, monkeypatch, capsys, recorded())
    assert code == 0, out
    assert out["signoff_metrics"]["antenna__violating__nets"]["status"] == "ok"


def test_gate_refuses_a_planted_antenna_count(tmp_path, monkeypatch, capsys):
    code, out = _gate(tmp_path, monkeypatch, capsys,
                      {**recorded(), "antenna__violating__nets": 3})
    assert code == 1, out
    assert kinds(out["violations"]) == ["antenna_violation"]


def test_gate_reports_missing_metrics_when_metrics_json_is_empty(
        tmp_path, monkeypatch, capsys):
    code, out = _gate(tmp_path, monkeypatch, capsys, {})
    assert code == 1, out
    assert set(kinds(out["violations"])) == {"signoff_metric_missing"}
    assert {r["status"] for r in out["signoff_metrics"].values()} == {"missing"}


def test_a_failed_flow_is_not_judged_on_its_partial_metrics(
        tmp_path, monkeypatch, capsys):
    def behavior(cwd):
        _flow({})(cwd)
        return subprocess.CompletedProcess(["eda"], 1, stdout="", stderr="boom")
    ws = make_ws(tmp_path)
    monkeypatch.setattr(check_harden.subprocess, "run", _fake_run_factory(behavior))
    code = check_harden.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    assert kinds(out["violations"]) == ["flow_step_failed"]
    assert out["signoff_metrics"] is None
