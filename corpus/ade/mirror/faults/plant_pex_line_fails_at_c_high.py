"""plant_pex_line_fails_at_c_high.py - ade/mirror `pex_sim` fault that only an
RC knob can see: the iout pin moves to the far end of a 17um run of 0.23um
metal1, and the spec asks for the c_high knob. DRC and LVS stay clean. The
output pole clears its 500 MHz floor at every PVT corner on the wires as
extracted (ss 509 MHz, the lowest) and falls under it at ss with every C
line x1.20 (486 MHz).
"""
from __future__ import annotations

from pathlib import Path

OLD = '''    d = iout_pad
    labels.append(("iout", (d[0] + d[2]) / 2, (d[1] + d[3]) / 2, L1LBL))
'''
NEW = '''    d = iout_pad
    y = (d[1] + d[3]) / 2
    layoutlib.rect(top, d[0], y - 0.115, d[2] + 17.0, y + 0.115, L1)
    layoutlib.rect(top, d[2] + 17.0, y - 0.5, d[2] + 18.0, y + 0.5, L1)
    labels.append(("iout", d[2] + 17.5, y, L1LBL))
'''


def plant(ws) -> None:
    gen_path = Path(ws) / "layout" / "gen_mirror.py"
    text = gen_path.read_text(encoding="utf-8")
    if OLD not in text:
        raise RuntimeError(
            f"{gen_path}: the iout label lines were not found as expected - "
            "gen_mirror.py's own source shape changed")
    gen_path.write_text(text.replace(OLD, NEW), encoding="utf-8")
    spec_path = Path(ws) / "spec" / "spec.yaml"
    spec_path.write_text(spec_path.read_text(encoding="utf-8")
                         + "pex_rc_knobs: [c_high]\n", encoding="utf-8")
