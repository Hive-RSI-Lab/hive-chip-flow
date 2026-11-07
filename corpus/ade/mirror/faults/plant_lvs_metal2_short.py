"""plant_lvs_metal2_short.py - ade/mirror `lvs` fault (#117): a short.

A metal2 strap from the last xmout unit's drain pad (iout, labelled) up to
the vss bus (labelled), landed by a via1 at each end: two labelled nets
joined in metal2, clear of the iout metal2 line below it. Both extractors must see iout and vss
merged, and klayout must locate the strap (STRAP below, metal2).
"""
from __future__ import annotations

from pathlib import Path

ANCHOR = "    # --- vdd: M8's pin with nothing on it inside the mirror\n"
NEW = (
    "    _cx = (out['d'][0] + out['d'][2]) / 2\n"
    "    _y0, _y1 = out['d'][3] - 0.5, (bus_y0 + bus_y1) / 2\n"
    "    layoutlib.via1(top, _cx, _y0)\n"
    "    layoutlib.via1(top, _cx, _y1)\n"
    "    layoutlib.rect(top, _cx - 0.2, _y0 - 0.2, _cx + 0.2, _y1 + 0.2,\n"
    "                   layoutlib.GF180_LAYER['metal2'])\n"
)
# the planted strap in the mirror's own frame (um): the last xmout unit's
# drain pad (unit 3 of the DBABD row) is x 9.9..10.28 up to y=3.72, the
# vss bus y 5.27..5.77
STRAP = (9.89, 3.02, 10.29, 5.72)


def plant(ws) -> None:
    gen_path = Path(ws) / "layout" / "gen_mirror.py"
    text = gen_path.read_text(encoding="utf-8")
    if ANCHOR not in text:
        raise RuntimeError(
            f"{gen_path}: the vdd section was not found as expected - "
            "gen_mirror.py's own source shape changed")
    gen_path.write_text(text.replace(ANCHOR, NEW + "\n" + ANCHOR),
                        encoding="utf-8")
