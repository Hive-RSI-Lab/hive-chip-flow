"""plant_lvs_missing_via1.py - ade/mirror `lvs` fault (#117): an open.

gen_mirror.py joins the row's bottom gate pads (iref_node) with one metal1
bus. Here that bus stops at xmref's drain drop, and a metal2 bridge carries
iref_node on to the last xmout unit's gate pad instead: a via1 lands the
bridge on the metal1 bus at xmref's end, and the via1 at the far end is
missing. That xmout unit's gate is left on a net of its own, so both extractors must see iref_node
split in two, and klayout must locate the bridge (BRIDGE below, metal2).
"""
from __future__ import annotations

from pathlib import Path

OLD = "    layoutlib.rect(top, g0[0], g0[1], g1[2], g0[3], L1)\n"
NEW = (
    "    layoutlib.rect(top, g0[0], g0[1], ref['d'][2], g0[3], L1)\n"
    "    _vy = (g0[1] + g0[3]) / 2\n"
    "    layoutlib.via1(top, ref['d'][2] - 0.2, _vy)\n"
    "    layoutlib.rect(top, ref['d'][2] - 0.4, g0[1], g1[2], g0[3],\n"
    "                   layoutlib.GF180_LAYER['metal2'])\n"
)
OLD_LABEL = 'labels.append(("iref_node", (d[2] + g1[0]) / 2'
NEW_LABEL = 'labels.append(("iref_node", (d[0] + d[2]) / 2'
# the planted metal2 bridge in the mirror's own frame (um), from
# fet_pads on the DBABD row (unit i at x = 3i): xmref's drain pad (unit 2)
# ends at x=7.28, the last xmout unit's bottom gate pad (unit 3) at
# x=9.63, the bottom gate pads span y -0.67..-0.29
BRIDGE = (6.88, -0.67, 9.63, -0.29)


def plant(ws) -> None:
    gen_path = Path(ws) / "layout" / "gen_mirror.py"
    text = gen_path.read_text(encoding="utf-8")
    if OLD not in text or OLD_LABEL not in text:
        raise RuntimeError(
            f"{gen_path}: the iref_node bus/label lines were not found as "
            "expected - gen_mirror.py's own source shape changed")
    text = text.replace(OLD, NEW).replace(OLD_LABEL, NEW_LABEL)
    gen_path.write_text(text, encoding="utf-8")
