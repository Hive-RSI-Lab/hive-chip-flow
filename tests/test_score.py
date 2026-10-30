"""bin/score.py: a known block scores the share of its check-all record's
steps that passed, are still fresh and carry no workaround."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import score  # noqa: E402


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


def test_no_record_scores_zero(tmp_path):
    b = _block(tmp_path, {"lint": {"status": "pass"}}, None)
    share, line = score.block_share(b, {})
    assert share == 0 and "check-all --record" in line


def test_a_record_from_other_sources_scores_zero(tmp_path, monkeypatch):
    monkeypatch.setattr(score, "sources", lambda b: "now")
    b = _block(tmp_path, {"lint": {"status": "pass"}}, {"top/lint": "pass"})
    share, line = score.block_share(b, {})
    assert share == 0 and "changed since check-all ran" in line


def test_share_counts_passed_and_waived_steps_with_no_workaround(tmp_path, monkeypatch):
    monkeypatch.setattr(score, "sources", lambda b: "now")
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
    assert share == 0.5 and "top/sim (stale)" in line


def test_speed_term_falls_with_cpu_and_needs_times():
    assert score.speed(1.0, None) == 0
    assert score.speed(1.0, 0) == 100
    assert score.speed(1.0, score.SPEED_R) == 50
    assert score.speed(0.5, score.SPEED_R) == 25
    assert score.speed(1.0, 100) > score.speed(1.0, 200)


def test_block_cpu_sums_the_records_step_times(tmp_path):
    b = _block(tmp_path, {"lint": {"status": "pass"}, "sim": {"status": "pass"}},
               {"top/lint": "pass", "top/sim": "pass"})
    assert score.block_cpu(b) is None
    rec = json.loads((b.path / score.RECORD).read_text())
    rec["step_cpu_s"] = {"top/lint": 1.5}
    (b.path / score.RECORD).write_text(json.dumps(rec))
    assert score.block_cpu(b) is None   # a step with no time: no speed
    rec["step_cpu_s"]["top/sim"] = 2.5
    (b.path / score.RECORD).write_text(json.dumps(rec))
    assert score.block_cpu(b) == 4.0


import score_faults  # noqa: E402


def test_every_corpus_manifest_reads():
    rungs = score_faults.read_rungs()
    assert {r.id for r in rungs} >= {"vde/uart", "ade/mirror", "msde/dac_tile"}
    assert all(r.faults for r in rungs)
    assert [r for r in rungs if r.id == "vde/counter8"][0].hardens()


def _rung(tmp_path) -> score_faults.Rung:
    return score_faults.Rung("vde", "toy", tmp_path, [("a", "lint"), ("b", "sim")])


def _report(*msgs, status=None):
    return {"status": status or ("violations" if msgs else "pass"),
            "violations": [{"msg": m} for m in msgs]}


def test_caught_counts_faults_no_problem_names(tmp_path):
    r = _rung(tmp_path)
    assert score_faults.caught(r, _report()) == {"a", "b"}
    assert score_faults.caught(r, _report("toy/b: expected gate status 'fail'")) == {"a"}
    # a failing reference proves no catch, nor does a run that errored
    assert score_faults.caught(r, _report("toy: untouched reference fails gate 'lint'")) == set()
    assert score_faults.caught(r, {"status": "error"}) == set()


def test_fault_share_caches_and_refuses_what_didnt_run(tmp_path, monkeypatch):
    monkeypatch.setenv("CHIP_FLOW_CACHE", str(tmp_path / "cache"))
    monkeypatch.setattr(score_faults, "fault_sources", lambda r, *a: "k-" + r.name)
    r = _rung(tmp_path)
    runs = []

    def runner(rung, timeout):
        runs.append(rung.name)
        return _report("toy/a: missed"), 1.0, ""

    assert score_faults.fault_share([r], budget=0, runner=runner)[:2] == (0, 2)
    assert runs == []
    got, planted, lines = score_faults.fault_share([r], budget=10, runner=runner)
    assert (got, planted, runs) == (1, 2, ["toy"]) and "missed: a" in lines[0]
    # cached: no second run, even with no budget
    assert score_faults.fault_share([r], budget=0, runner=runner)[:2] == (1, 2)
    assert runs == ["toy"]
    # a run that left no report counts nothing and isn't cached
    r2 = score_faults.Rung("vde", "two", tmp_path, [("c", "lint")])
    got, _, lines = score_faults.fault_share(
        [r2], budget=10, runner=lambda rung, t: (None, 5.0, "killed after 1 s"))
    assert got == 0 and "killed" in lines[0]
    assert not (tmp_path / "cache" / "faults" / "k-two.json").exists()


def test_fault_sources_moves_with_the_rung(tmp_path):
    r = [x for x in score_faults.read_rungs() if x.id == "vde/spi_fifo"][0]
    a = score_faults.fault_sources(r)
    other = [x for x in score_faults.read_rungs() if x.id == "vde/uart"][0]
    assert a != score_faults.fault_sources(other)
    assert a == score_faults.fault_sources(r)
