"""Fault: the analog spec declares a timing abstract and leaves out a crossing
pin's limits (gates.yaml's msde macro_lib row). max_transition is the analog
side's to state, so the gate must refuse rather than invent one or leave the pin
unconstrained in a liberty that looks complete."""
from pathlib import Path

import yaml


def plant(ws: Path) -> None:
    path = ws / "analog" / "spec" / "spec.yaml"
    spec = yaml.safe_load(path.read_text(encoding="utf-8"))
    spec["macro_timing"] = {"pins": {}, "arcs": []}
    path.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")
