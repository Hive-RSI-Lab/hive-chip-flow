"""plant_drc_antenna.py - ade/mirror `drc` fault (#119): one more minimum nfet
beside the mirror, whose gate hangs off a 120 um metal1 strap with no diode.

`plant` leaves that gate net without a pin, so its ratio is final and the
antenna deck's finding is an error. `plant_on_pin` ties the same gate to the
vdd pin, whose ratio the chip's routing still changes, so it is an info. Nothing
else about the layout changes, so the other DRC rules still pass.
"""
from __future__ import annotations

from pathlib import Path

ANCHOR = "    # magic numbers a cell's ports in the order"
STRAP_UM = 120.0

FET = f"""    ant_cell = draw_fet.draw_nfet(l_gate=0.28, w_gate=0.5)
    ant_dx = vdd_x + 4.0
    ant_dy = bus_y0 - 0.79
    top.add_ref(ant_cell).move((ant_dx, ant_dy))
    ant_pads = layoutlib.fet_pads(ant_cell)
    g_bot = ant_pads["gate_bot"]
    layoutlib.rect(top, g_bot[0] + ant_dx, g_bot[1] + ant_dy - {STRAP_UM},
                   g_bot[2] + ant_dx, g_bot[3] + ant_dy, L1)
"""

LABEL = """    labels_gate = ant_pads["gate_top"]
    labels.append(("ant_gate", labels_gate[0] + ant_dx + 0.1,
                   labels_gate[1] + ant_dy + 0.1, L1LBL))
"""

TIE = """    g_top = ant_pads["gate_top"]
    layoutlib.rect(top, vdd_x + 1.0, bus_y0, g_top[0] + ant_dx + 0.38, bus_y1, L1)
"""

SORT = "labels.sort(key=lambda lbl: PINS.index(lbl[0]))"
SORT_KEEPS_EXTRA = ("labels.sort(key=lambda lbl: PINS.index(lbl[0]) "
                    "if lbl[0] in PINS else len(PINS))")


def _edit(ws, block: str) -> None:
    gen_path = Path(ws) / "layout" / "gen_mirror.py"
    text = gen_path.read_text(encoding="utf-8")
    if ANCHOR not in text or SORT not in text:
        raise RuntimeError(
            f"{gen_path}: anchor lines not found as expected - gen_mirror.py's "
            "own source shape changed")
    text = text.replace(ANCHOR, block + ANCHOR).replace(SORT, SORT_KEEPS_EXTRA)
    gen_path.write_text(text, encoding="utf-8")


def plant(ws) -> None:
    _edit(ws, FET + LABEL)


def plant_on_pin(ws) -> None:
    _edit(ws, FET + TIE)
