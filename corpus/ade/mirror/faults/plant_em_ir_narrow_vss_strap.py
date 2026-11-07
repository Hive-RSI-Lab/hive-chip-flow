"""plant_em_ir_narrow_vss_strap.py - ade/mirror `em_ir` fault, gates.yaml's
own: "a supply strap narrowed to minimum width". The vss pin moves from the
bus above the row to the far end, past the left tap, of a 1000um strap of
0.23um (M1.1's minimum width) metal1. DRC and LVS stay clean - it is the
same net, legally drawn - but every source's current now crosses about
390 ohm of strap, so the sources sit over 10 mV above the pin: far over the
2 mV spec.yaml allows the mirror's sources to drift (ir_drop_mv).
"""
from __future__ import annotations

from pathlib import Path

OLD = '''    labels.append(("vss", (ref["s"][2] + ref["d"][0]) / 2,
                   (bus_y0 + bus_y1) / 2, L1LBL))
'''
NEW = '''    y = (bus_y0 + bus_y1) / 2
    x0 = tap_xl + 0.2
    layoutlib.rect(top, x0 - 1000.0, y - 0.115, x0, y + 0.115, L1)
    layoutlib.rect(top, x0 - 1001.0, y - 0.5, x0 - 1000.0, y + 0.5, L1)
    labels.append(("vss", x0 - 1000.5, y, L1LBL))
'''


def plant(ws) -> None:
    gen_path = Path(ws) / "layout" / "gen_mirror.py"
    text = gen_path.read_text(encoding="utf-8")
    if OLD not in text:
        raise RuntimeError(
            f"{gen_path}: the vss label lines were not found as expected - "
            "gen_mirror.py's own source shape changed")
    gen_path.write_text(text.replace(OLD, NEW), encoding="utf-8")
