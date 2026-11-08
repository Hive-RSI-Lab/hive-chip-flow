"""Fault: a feedback block with no stability measure (issue #100's
spec_lint_ade rule) - spec.yaml still says `feedback: true`, but its
pm_deg measure is gone, so nothing bounds the loop's phase margin."""
from pathlib import Path

import yaml


def plant(ws: Path) -> None:
    p = ws / "spec" / "spec.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    data["measures"] = [m for m in data["measures"] if m["name"] != "pm_deg"]
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
