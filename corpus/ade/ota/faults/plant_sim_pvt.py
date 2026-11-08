"""Fault: a compensation cap too small (issue #100) - cc_w 24 um instead of
32 um, so C falls from about 2.0 pF to 1.15 pF. Typical still meets the
60 degree phase margin (about 61), but the fast corners lose it (ff about
58.6, fs and tt_pff about 59): a sizing that meets at typical and fails at
PVT, gates.yaml's sim_pvt row."""
from pathlib import Path

import yaml


def plant(ws: Path) -> None:
    p = ws / "sizing" / "sizing.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    data["cc_w"]["value"] = 24.0e-6
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
