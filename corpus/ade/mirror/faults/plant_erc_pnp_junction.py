"""plant_erc_pnp_junction.py - ade/mirror `erc` fault: the PDK's vertical
PNP (pnp_05p00x05p00) is added beside the vdd pad with its base wired over
metal2 to the vss tap, its collector the p-substrate (so vss through the
mirror's psub tap), and its emitter on a new pin vbat that spec.yaml puts
at 0..5 V, as when the PNP senses a battery rail. The PNP is a 3.3 V LV
device (junctions rated 3.63 V), so CVC sees its emitter-base and
collector-emitter junctions over the rating, and the erc gate names the
PNP with vbat and vss.
"""
from __future__ import annotations

from pathlib import Path

PINS_OLD = 'PINS = ["iref_node", "iout", "vdd", "vss"]'
PINS_NEW = 'PINS = ["iref_node", "iout", "vdd", "vss", "vbat"]'
ANCHOR = "    # magic numbers a cell's ports in the order their labels were written,"
# the cell's own frame: a 5x5 emitter at 1.7..6.7, the base ring 0.96..7.44
# and the collector ring 0.22..8.18, each ring 0.42 wide and closed, so the
# emitter and base leave over metal2.
ADD = '''    # --- planted: a vertical PNP, emitter on vbat, base on vss
    pnp = gf.import_gds(layoutlib.pdk_root() / "libs.tech" / "klayout"
                        / "tech" / "pymacros" / "cells" / "bjt"
                        / "pnp_05p00x05p00.gds")
    tap_cy = tap_y + TAP_SIZE / 2
    qx, qy = vdd_x + 4.0, tap_cy - 4.2
    top.add_ref(pnp).move((qx, qy))
    m2 = layoutlib.GF180_LAYER["metal2"]
    # base: the ring's left side, wired level with the tap to its via
    layoutlib.via1(top, tap_x + TAP_SIZE / 2, tap_cy)
    layoutlib.via1(top, qx + 1.17, tap_cy)
    layoutlib.rect(top, tap_x + TAP_SIZE / 2, tap_cy - 0.2,
                   qx + 1.17, tap_cy + 0.2, m2)
    # emitter: its centre, up over both rings to a metal1 pad, vbat
    ex, top_y = qx + 4.2, qy + 9.5
    layoutlib.via1(top, ex, tap_cy)
    layoutlib.via1(top, ex, top_y)
    layoutlib.rect(top, ex - 0.2, tap_cy, ex + 0.2, top_y, m2)
    layoutlib.rect(top, ex - 0.5, top_y - 0.5, ex + 0.5, top_y + 0.5, L1)
    labels.append(("vbat", ex, top_y, L1LBL))

'''
SPEC_OLD = "  iref_node: {min: 0, max: 0.6}\n"
SPEC_NEW = SPEC_OLD + "  vbat: {min: 0, max: 5.0}\n"


def plant(ws) -> None:
    ws = Path(ws)
    path = ws / "layout" / "gen_mirror.py"
    text = path.read_text(encoding="utf-8")
    if ANCHOR not in text or PINS_OLD not in text or "vdd_x = " not in text:
        raise RuntimeError(f"{path}: PINS line or label-sort anchor not found "
                           "as expected - gen_mirror.py's own source shape "
                           "changed")
    text = text.replace(PINS_OLD, PINS_NEW).replace(ANCHOR, ADD + ANCHOR)
    path.write_text(text, encoding="utf-8")
    spec = ws / "spec" / "spec.yaml"
    text = spec.read_text(encoding="utf-8")
    if SPEC_OLD not in text:
        raise RuntimeError(f"{spec}: erc_nets iref_node line not found as "
                           "expected - spec.yaml's shape changed")
    spec.write_text(text.replace(SPEC_OLD, SPEC_NEW), encoding="utf-8")
