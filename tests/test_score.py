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

