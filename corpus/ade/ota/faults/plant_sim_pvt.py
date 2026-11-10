"""Fault: a compensation cap too small (issues #100, #297) - cc_w 22 um
instead of 26 um, so C falls from about 1.35 pF to 0.97 pF. Typical still
meets every bound in both conditions (through the pad: PM about 73, GM
about 10.9 dB, against 10), but the pad condition loses its gain margin at
the fast corners (tt_pff about 8.8 dB, sf about 9.8): a sizing that meets
at typical and fails at PVT, gates.yaml's sim_pvt row."""
from pathlib import Path

import yaml


def plant(ws: Path) -> None:
    p = ws / "sizing" / "sizing.yaml"
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    data["cc_w"]["value"] = 22.0e-6
    p.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
