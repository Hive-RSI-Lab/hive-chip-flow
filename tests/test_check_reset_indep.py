"""engine/scripts/check_reset_indep.py: the reset_indep gate (#84,
docs/design.md 1.5). The comparison is tested on its own; the slow tests
run corpus counter8 for real on Verilator, clean and with the planted
unreset register, and show the plant passes the Icarus sim gate."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))
sys.path.insert(0, str(REPO / "corpus" / "vde" / "counter8" / "faults"))

import check_reset_indep as ri  # noqa: E402
import check_sim  # noqa: E402
import faults  # noqa: E402
import plant_reset_indep  # noqa: E402

RUNG = REPO / "corpus" / "vde" / "counter8"


def _ok():
    return {"passed": True, "skipped": False, "message": None}


def _bad(msg="assert 0 == 1\n detail"):
    return {"passed": False, "skipped": False, "message": msg}


def _kinds(vs):
    return [(v["kind"], v["severity"], v["module"]) for v in vs]


def test_same_results_at_every_init_is_clean():
    runs = {None: {"t1": _ok()}, 1: {"t1": _ok()}, 2: {"t1": _ok()}}
    assert ri.compare(runs) == []


def test_pass_at_zero_fail_at_a_seed_is_reset_dependent():
    runs = {None: {"t1": _ok(), "t2": _ok()},
            1: {"t1": _ok(), "t2": _ok()},
            2: {"t1": _bad(), "t2": _ok()},
            3: {"t1": _bad(), "t2": _ok()}}
    vs = ri.compare(runs, {"t1": {"R1"}})
    assert _kinds(vs) == [("reset_dependent", "error", "t1")]
    assert vs[0]["init_seeds"] == [2, 3]
    assert vs[0]["refs"] == ["R1"]
    # the first line of the failure only
    assert "detail" not in vs[0]["msg"] and "assert 0 == 1" in vs[0]["msg"]


def test_test_missing_from_a_seeded_run_is_reset_dependent():
    runs = {None: {"t1": _ok()}, 1: {}}
    assert _kinds(ri.compare(runs)) == [("reset_dependent", "error", "t1")]


def test_fail_at_zero_is_a_warning_left_out_of_the_comparison():
    runs = {None: {"t1": _bad()}, 1: {"t1": _ok()}}
    assert _kinds(ri.compare(runs)) == [
        ("verilator_baseline_failed", "warning", "t1")]


def test_skipped_test_is_info():
    skipped = {"passed": False, "skipped": True, "message": "skip"}
    runs = {None: {"t1": _ok()}, 1: {"t1": skipped}}
    assert _kinds(ri.compare(runs)) == [("test_skipped", "info", "t1")]


def test_init_plusargs():
    assert ri.init_plusargs(None) == ["+verilator+rand+reset+0"]
    assert ri.init_plusargs(7) == ["+verilator+rand+reset+2",
                                   "+verilator+seed+7"]


def test_no_tb_modules_is_an_error(tmp_path, capsys):
    ws = faults.make_scratch_workspace(tmp_path, RUNG, "vde", "counter8")
    for p in (ws / "tb").glob("test_*.py"):
        p.unlink()
    assert ri.main(["--workspace", str(ws)]) == 2
    assert "no test_*.py" in json.loads(capsys.readouterr().out)["error"]


def _run(main, ws, capsys):
    code = main(["--workspace", str(ws)])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.slow
def test_clean_counter8_passes_and_leaves_tb_alone(tmp_path, capsys):
    ws = faults.make_scratch_workspace(tmp_path, RUNG, "vde", "counter8")
    before = sorted(p.name for p in (ws / "tb").iterdir())
    code, out = _run(ri.main, ws, capsys)
    assert (code, out["status"]) == (0, "pass"), out
    assert out["tests"] > 0 and out["init_seeds"] == list(ri.INIT_SEEDS)
    assert sorted(p.name for p in (ws / "tb").iterdir()) == before


@pytest.mark.slow
def test_unreset_register_passes_sim_and_fails_reset_indep(tmp_path, capsys):
    ws = faults.make_scratch_workspace(tmp_path, RUNG, "vde", "counter8")
    plant_reset_indep.plant(ws)
    code, out = _run(check_sim.main, ws, capsys)
    assert (code, out["status"]) == (0, "pass"), out
    code, out = _run(ri.main, ws, capsys)
    assert code == 1, out
    kinds = {v["kind"] for v in out["violations"] if v["severity"] == "error"}
    assert kinds == {"reset_dependent"}
