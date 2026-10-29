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


def test_a_pass_with_no_inputs_to_check_is_not_clean(tmp_path):
    # a recorded pass whose inputs can't be hashed against the files now
    # (none recorded) is not fresh, so it doesn't count
    b = _block(tmp_path, {"lint": {"status": "pass", "last": {"inputs": {}}}},
               {"top/lint": "pass"})
    share, line = score.block_share(b, {})
    assert share == 0 and "stale since" in line


def test_failed_step_stays_a_step(tmp_path):
    b = _block(tmp_path, {"lint": {"status": "fail"}, "sim": {"status": "pass"}}, None)
    assert b.steps() == ["top/lint", "top/sim"]

