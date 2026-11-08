"""Fault: stability bounds wide enough to pass anything (gates.yaml's
bench_strength row) - pm_deg, gm_db and ugf_hz in tb/ota_stb_tb.bounds.json
open to ranges no output of this design leaves, and pm_deg loses its
sensitivity. Halving the Miller cap then moves only the loop's margins and
speed, which nothing bounds any more, so the cap_halved mutant survives."""
import json
from pathlib import Path

WIDE = {"pm_deg": {"min": -360.0}, "gm_db": {"min": -1000.0},
        "ugf_hz": {"min": 0.0}}


def plant(ws: Path) -> None:
    p = ws / "tb" / "ota_stb_tb.bounds.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    for entry in data:
        if entry["measure"] in WIDE:
            for k in ("min", "max", "sensitivity"):
                entry.pop(k, None)
            entry.update(WIDE[entry["measure"]])
    p.write_text(json.dumps(data), encoding="utf-8")
