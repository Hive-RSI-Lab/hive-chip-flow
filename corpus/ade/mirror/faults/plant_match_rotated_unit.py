"""plant_match_rotated_unit.py - ade/mirror `layout_match` fault (#118):
xmout's right unit (row position 3) turned 180 degrees about its own
centre, its source and drain pads (and gate straps) swapped so the wiring
still lands on the right terminals. DRC, LVS and the centroids all hold;
the unit's current now runs the other way to every other unit's.
"""
from __future__ import annotations

from pathlib import Path

OLD = """        top.add_ref(cells[kind]).move((x, 0.0))
        pads = {k: shifted(v, x) for k, v in layoutlib.fet_pads(cells[kind]).items()}
"""
NEW = """        r = top.add_ref(cells[kind]).move((x, 0.0))
        pads = {k: shifted(v, x) for k, v in layoutlib.fet_pads(cells[kind]).items()}
        if i == 3:
            b = pads["gate_top"]
            r.rotate(180, center=((pads["s"][0] + pads["d"][2]) / 2,
                                  (pads["gate_bot"][1] + b[3]) / 2))
            pads["s"], pads["d"] = pads["d"], pads["s"]
            pads["gate_top"], pads["gate_bot"] = pads["gate_bot"], pads["gate_top"]
"""


def plant(ws) -> None:
    gen_path = Path(ws) / "layout" / "gen_mirror.py"
    text = gen_path.read_text(encoding="utf-8")
    if OLD not in text:
        raise RuntimeError(f"{gen_path}: the row loop was not found as "
                           "expected - gen_mirror.py's own source shape changed")
    gen_path.write_text(text.replace(OLD, NEW), encoding="utf-8")
