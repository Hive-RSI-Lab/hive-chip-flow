"""The slow workflow (#343) runs every part tests/check-slow.sh has: a part
missing from its matrix would never run on CI."""
from __future__ import annotations

import re
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


def test_slow_ci_matrix_runs_every_check_slow_part():
    script = (REPO / "tests" / "check-slow.sh").read_text(encoding="utf-8")
    parts = re.search(r'^SHARDS="([^"]*)"', script, re.M).group(1).split()
    wf = yaml.safe_load(
        (REPO / ".github" / "workflows" / "slow.yml").read_text(
            encoding="utf-8"))
    matrix = wf["jobs"]["slow"]["strategy"]["matrix"]["part"]
    assert sorted(matrix) == sorted(parts), (
        f"slow.yml matrix {sorted(matrix)} != check-slow.sh SHARDS "
        f"{sorted(parts)}: a part missing there never runs on CI")
    run = wf["jobs"]["slow"]["steps"][-1]["run"]
    assert "tests/check-slow.sh" in run and "${{ matrix.part }}" in run
