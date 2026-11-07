"""plant_match_aabb.py - ade/mirror `layout_match` fault (#118): the row
drawn D A B B D instead of D B A B D. Every unit is still wired to its own
pads, so DRC, both LVS extractors (which merge xmout's parallel units) and
the pex bench all pass; only the centroids move apart, xmout's 4.5 um
right of xmref's.
"""
from __future__ import annotations

from pathlib import Path

OLD = 'ROW = "DBABD"\n'
NEW = 'ROW = "DABBD"\n'


def plant(ws) -> None:
    gen_path = Path(ws) / "layout" / "gen_mirror.py"
    text = gen_path.read_text(encoding="utf-8")
    if OLD not in text:
        raise RuntimeError(f"{gen_path}: {OLD.strip()} was not found as "
                           "expected - gen_mirror.py's own source shape changed")
    gen_path.write_text(text.replace(OLD, NEW), encoding="utf-8")
