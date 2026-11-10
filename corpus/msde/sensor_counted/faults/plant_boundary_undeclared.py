"""Fault: a macro with crossing pins and no `boundary:` section."""
from pathlib import Path

import yaml


def plant(ws: Path) -> None:
    path = ws / "analog" / "spec" / "spec.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    del data["boundary"]
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
