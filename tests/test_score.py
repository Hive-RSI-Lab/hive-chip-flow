"""bin/score.py: a known block scores the share of its gate steps that a
run on what they read now passed (the step cache, else the committed seed)
and that carry no workaround; bin/check_all.py fills the cache."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import score  # noqa: E402

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _cache(tmp_path, monkeypatch):
    """Every test its own step cache: never the user's."""
    monkeypatch.setenv("CHIP_FLOW_CACHE", str(tmp_path / "cache"))


def _block(tmp_path: Path, gates: dict, steps: dict | None) -> score.Block:
    d = tmp_path / "b"
    d.mkdir()
    (d / "state.json").write_text(json.dumps({"skill": "vde", "block": "b", "gates": gates}))
    if steps is not None:
        (d / score.RECORD).write_text(json.dumps({"ran": "t", "toolchain": "x", "steps": steps}))
    return score.read_blocks(tmp_path)[0]


def test_known_blocks_are_pinned():
    known = score.read_known(score.KNOWN)
    assert {k["name"] for k in known} >= {"r2r_dac", "counter"}


def _now(monkeypatch):
    monkeypatch.setattr(score, "sources", lambda b: "now")
    monkeypatch.setattr(score, "step_sources", lambda b, steps: {s: "h-" + s for s in steps})


def test_no_run_scores_zero(tmp_path, monkeypatch):
    _now(monkeypatch)
    b = _block(tmp_path, {"lint": {"status": "pass"}}, None)
    share, line = score.block_share(b, {})
    assert share == 0 and "top/lint (not run on these inputs)" in line


def test_a_record_from_other_sources_scores_zero(tmp_path, monkeypatch):
    _now(monkeypatch)
    b = _block(tmp_path, {"lint": {"status": "pass"}}, {"top/lint": "pass"})
    share, line = score.block_share(b, {})
    assert share == 0 and "not run on these inputs" in line


def test_a_cached_run_on_these_inputs_counts_with_no_record(tmp_path, monkeypatch):
    _now(monkeypatch)
    b = _block(tmp_path, {"lint": {}, "sim": {}}, None)
    score.cache_put("b", "top/lint", "h-top/lint", "pass", 3, "tree")
    score.cache_put("b", "top/sim", "other inputs", "pass", 3, "tree")
    share, line = score.block_share(b, {})
    assert share == 0.5 and "top/sim (not run on these inputs)" in line
    score.cache_put("b", "top/sim", "h-top/sim", "fail", 3, "tree")
    share, line = score.block_share(b, {})
    assert share == 0.5 and "top/sim (fail)" in line


def test_the_cache_wins_over_the_seed(tmp_path, monkeypatch):
    _now(monkeypatch)
    b = _block(tmp_path, {"lint": {}}, {"top/lint": "fail"})
    rec = json.loads((b.path / score.RECORD).read_text())
    (b.path / score.RECORD).write_text(json.dumps({**rec, "step_sources": {"top/lint": "h-top/lint"}}))
    assert score.block_share(b, {})[0] == 0
    score.cache_put("b", "top/lint", "h-top/lint", "pass", 3, "tree")
    assert score.block_share(b, {})[0] == 1


def test_a_waived_gate_is_a_step_in_pipeline_order(tmp_path):
    b = _block(tmp_path, {"release": {}, "lint": {}}, None)
    (b.path / "reports").mkdir()
    (b.path / "reports" / "waivers.json").write_text(json.dumps([{"gate": "timing"}]))
    steps = [(w.tag, g, waived) for w, g, waived in score.plan(b)]
    assert steps == [("top", "lint", False), ("top", "timing", True), ("top", "release", False)]


def test_plan_orders_as_the_engine_does():
    sys.path[:0] = [str(ROOT / "engine" / "scripts"), str(ROOT / "engine" / "lib")]
    import state
    rows = score.gate_rows(ROOT / "engine" / "reference" / "gates.yaml")
    for skill in ("vde", "ade", "msde"):
        assert [g for g, _, _ in rows[skill]] == \
            [g for _, g in state.applicable_gate_order(skill)], skill


def test_share_counts_passed_and_waived_steps_with_no_workaround(tmp_path, monkeypatch):
    _now(monkeypatch)
    steps = {"top/lint": "pass", "top/sim": "waived", "top/synth": "fail", "top/harden": "pass"}
    b = _block(tmp_path, {g.split("/")[1]: {"status": "pass"} for g in steps}, steps)
    rec = json.loads((b.path / score.RECORD).read_text())
    (b.path / score.RECORD).write_text(json.dumps({**rec, "sources": "now"}))
    share, line = score.block_share(b, {})
    assert share == 0.75 and "top/synth (fail)" in line
    shim = score.Workaround("w", "shim", ["b"], ["top/harden"])
    share, line = score.block_share(b, {"w": shim})
    assert share == 0.5 and "top/harden (w)" in line


def test_failed_step_stays_a_step(tmp_path):
    b = _block(tmp_path, {"lint": {"status": "fail"}, "sim": {"status": "pass"}}, None)
    assert b.steps() == ["top/lint", "top/sim"]



def _repo(tmp_path: Path, monkeypatch) -> Path:
    """A git tree with a two-gate engine and one vde block: gate.py imports
    checklib; check_sim.py reads reference/sim.yaml and runs helper.py."""
    import subprocess
    files = {
        "bin/eda": "#!/bin/sh\n",
        "engine/lib/checklib.py": "X = 1\n",
        "engine/scripts/gate.py": '"""Runs check_<tool>.py; gates.yaml."""\nimport checklib\n'
                                  'G = "gates.yaml"\n',
        "engine/scripts/check_lint.py": "import checklib\n",
        "engine/scripts/check_sim.py": 'from checklib import X\nY = ("reference", "sim.yaml")\n'
                                       'H = "helper.py"\n',
        "engine/scripts/helper.py": "",
        "engine/scripts/unused.py": "",
        "engine/reference/gates.yaml": "gates:\n  vde:\n    lint:\n      tool: lint\n"
                                       "    sim:\n      tool: sim\n",
        "engine/reference/sim.yaml": "a: 1\n",
        "engine/reference/nobody.json": "{}\n",
        "blocks/b/state.json": json.dumps({"skill": "vde", "block": "b",
                                           "gates": {"lint": {}, "sim": {}}}),
    }
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    monkeypatch.setattr(score, "ROOT", tmp_path)
    return tmp_path


def _stale_after(root: Path, rel: str) -> set[str]:
    b = score.read_blocks(root / "blocks")[0]
    before = score.step_sources(b, ["top/lint", "top/sim"])
    (root / rel).write_text((root / rel).read_text() + "# edit\n")
    after = score.step_sources(b, ["top/lint", "top/sim"])
    return {s for s in before if before[s] != after[s]}


def test_an_engine_file_no_gate_reads_stales_nothing(tmp_path, monkeypatch):
    assert _stale_after(_repo(tmp_path, monkeypatch), "engine/scripts/unused.py") == set()


def test_a_gates_own_script_and_what_it_reads_stale_it(tmp_path, monkeypatch):
    root = _repo(tmp_path, monkeypatch)
    for rel in ("engine/scripts/check_sim.py", "engine/reference/sim.yaml",
                "engine/scripts/helper.py"):
        assert _stale_after(root, rel) == {"top/sim"}, rel


def test_an_earlier_steps_engine_stales_the_steps_after_it(tmp_path, monkeypatch):
    assert _stale_after(_repo(tmp_path, monkeypatch), "engine/scripts/check_lint.py") == \
        {"top/lint", "top/sim"}


def test_gate_its_libraries_the_block_and_unnamed_data_stale_every_step(tmp_path, monkeypatch):
    root = _repo(tmp_path, monkeypatch)
    for rel in ("engine/scripts/gate.py", "engine/lib/checklib.py", "engine/reference/gates.yaml",
                "engine/reference/nobody.json", "bin/eda", "blocks/b/state.json"):
        assert _stale_after(root, rel) == {"top/lint", "top/sim"}, rel


def test_a_step_recorded_stale_is_not_clean(tmp_path, monkeypatch):
    root = _repo(tmp_path, monkeypatch)
    b = score.read_blocks(root / "blocks")[0]
    steps = {"top/lint": "pass", "top/sim": "pass"}
    rec = {"ran": "t", "steps": steps, "step_sources": score.step_sources(b, list(steps))}
    (b.path / score.RECORD).write_text(json.dumps(rec))
    assert score.block_share(b, {})[0] == 1
    with (root / "engine/scripts/check_sim.py").open("a") as f:
        f.write("Z = 2\n")
    share, line = score.block_share(b, {})
    assert share == 0.5 and "top/sim (not run on these inputs)" in line


def _check_all(root: Path, monkeypatch, run_gate, *args: str) -> int:
    import check_all
    monkeypatch.setattr(check_all, "ROOT", root)
    monkeypatch.setattr(check_all, "run_gate", run_gate)
    monkeypatch.setattr(sys, "argv", ["check_all", "--no-probes", *args])
    return check_all.main()


def test_check_all_fills_the_cache_the_score_reads(tmp_path, monkeypatch):
    root = _repo(tmp_path, monkeypatch)
    ran = []

    def gate(ws, skill, g, logs, tag, timeout=None):
        ran.append(g)
        return True, "pass"
    assert _check_all(root, monkeypatch, gate) == 0
    b = score.read_blocks(root / "blocks")[0]
    assert ran == ["lint", "sim"] and score.block_share(b, {})[0] == 1
    assert not (b.path / score.RECORD).exists()
    # --stale: everything is covered, so nothing runs
    assert _check_all(root, monkeypatch, gate, "--stale") == 0 and ran == ["lint", "sim"]
    with (root / "engine/scripts/check_sim.py").open("a") as f:
        f.write("Z = 2\n")
    assert _check_all(root, monkeypatch, gate, "--stale") == 0
    assert ran == ["lint", "sim", "lint", "sim"] and score.block_share(b, {})[0] == 1


def test_a_step_cut_off_by_the_budget_is_not_clean(tmp_path, monkeypatch):
    import check_all
    root = _repo(tmp_path, monkeypatch)

    def gate(ws, skill, g, logs, tag, timeout=None):
        assert timeout is not None and timeout > 0
        if g == "sim":
            raise check_all.OutOfTime
        return True, "pass"
    assert _check_all(root, monkeypatch, gate, "--budget", "600", "--record") == 1
    b = score.read_blocks(root / "blocks")[0]
    share, line = score.block_share(b, {})
    assert share == 0.5 and "top/sim (not run on these inputs)" in line
    assert not (b.path / score.RECORD).exists()   # a cut-off run is no seed
