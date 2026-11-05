"""Fault: an analog macro with crossing pins and no macro_timing (#177).

Every macro with a crossing pin owes the macro_lib gate: with the declaration
taken out of the analog spec there is no lib view for STA, and top_harden must
fail macro_lib_missing before any harden runs, not time the pins as ideal
ports."""
from pathlib import Path

import yaml


def plant(ws: Path) -> None:
    spec = ws / "analog" / "spec" / "spec.yaml"
    data = yaml.safe_load(spec.read_text(encoding="utf-8"))
    if not data.pop("macro_timing", None):
        raise RuntimeError("plant_top_harden_lib_undeclared.py: the analog "
                           "spec declares no macro_timing to take out")
    spec.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
