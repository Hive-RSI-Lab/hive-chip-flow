"""Fault: the nested digital spec's crossing port at a different width from
interface.yaml, the root specs still agreeing."""
from pathlib import Path

import yaml


def plant(ws: Path) -> None:
    path = ws / "digital" / "spec" / "spec.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["ports"]["osc_en"]["width"] = 2
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
