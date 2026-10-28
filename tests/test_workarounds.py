"""docs/workarounds.md matches its probes (tests/workarounds/*.py).

The score (bin/chip-flow score) reads that table, so it is held to the truth
on every change: a row whose probe says its cause is fixed, or a cause that
stands with no row, fails here. bin/check_all.py runs the same probes after
it re-runs every block; this is them without the blocks.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

import score  # noqa: E402


def test_workarounds_table_matches_its_probes():
    table = score.read_workarounds(ROOT / "docs" / "workarounds.md")
    assert score.probes(table) == []


def test_every_block_step_is_named_as_the_table_names_steps():
    for b in score.read_blocks(ROOT / "blocks"):
        assert b.workspaces, b.name
        for s in b.steps():
            assert s.split("/")[0] in ("top", "analog", "digital"), s
