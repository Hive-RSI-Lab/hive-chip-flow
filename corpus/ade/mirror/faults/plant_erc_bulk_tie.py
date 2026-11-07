"""plant_erc_bulk_tie.py - ade/mirror `erc` fault: a PMOS (pfet_03v3) is
added beside the vdd pad with its source, drain and gate on vdd, and the
N+ tap of its nwell wired over metal2 to the vss tap, so its bulk sits at
0 V under a 3.3 V source: the wrong-rail well tie. CVC reports PMOS
source vs bulk, and the erc gate names the pfet.
"""
from __future__ import annotations

from pathlib import Path

ANCHOR = "    # magic numbers a cell's ports in the order their labels were written,"
ADD = '''    # --- planted: a pfet on vdd whose nwell tap is wired to vss
    pcell = draw_fet.draw_pfet(l_gate=GATE_L, w_gate=MREF_W)
    px = vdd_x + 4.0
    top.add_ref(pcell).move((px, 0.0))
    pp = {k: shifted(v, px) for k, v in layoutlib.fet_pads(pcell).items()}
    p_x1 = max(b[2] for b in pp.values())
    p_y0 = min(b[1] for b in pp.values())
    # one metal1 sheet over vdd's pad and every pfet pad: s = d = g = vdd
    layoutlib.rect(top, vdd_x, p_y0, p_x1, bus_y1, L1)
    ntap_x = p_x1 + 2.0
    ntap = top.add_ref(layoutlib.nwell_tap(TAP_SIZE))
    ntap.move((ntap_x, tap_y))
    layoutlib.rect(top, px - 1.5, p_y0 - 1.5, ntap_x + TAP_SIZE + 1.5,
                   bus_y1 + 0.5, layoutlib.GF180_LAYER["nwell"])
    layoutlib.via1(top, ntap_x + TAP_SIZE / 2, tap_y + TAP_SIZE / 2)
    layoutlib.via1(top, tap_x + TAP_SIZE / 2, tap_y + TAP_SIZE / 2)
    layoutlib.rect(top, tap_x + 0.3, tap_y + 0.3, ntap_x + TAP_SIZE - 0.3,
                   tap_y + TAP_SIZE - 0.3, layoutlib.GF180_LAYER["metal2"])

'''


def plant(ws) -> None:
    path = Path(ws) / "layout" / "gen_mirror.py"
    text = path.read_text(encoding="utf-8")
    if ANCHOR not in text or "vdd_x = " not in text:
        raise RuntimeError(f"{path}: label-sort anchor not found as expected "
                           "- gen_mirror.py's own source shape changed")
    path.write_text(text.replace(ANCHOR, ADD + ANCHOR), encoding="utf-8")
