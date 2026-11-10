"""Fault: a d2a pin declared with no net to watch: the gate cannot measure
its switching point, so it refuses rather than passes."""
from pathlib import Path

import yaml


def plant(ws: Path) -> None:
    path = ws / "analog" / "spec" / "spec.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    del data["boundary"]["d2a"]["osc_en"]["observe"]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
