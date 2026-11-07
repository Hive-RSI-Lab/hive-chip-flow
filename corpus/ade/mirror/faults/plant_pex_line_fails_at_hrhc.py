"""plant_pex_line_fails_at_hrhc.py - ade/mirror `pex_sim` fault only a real RC
corner sees: the iout pin moves to the far end of a 18.5um run of 0.23um metal1,
and the spec asks for every magic RC variant. DRC and LVS stay clean. The output
pole clears its 500 MHz floor at every PVT corner on the wires as extracted (ss
504.8 MHz, the lowest) and at lrlc @ ff (614.0), hrlc @ sf (523.5) and
lrhc @ fs (578.6). It falls under the floor only at hrhc @ ss (497.7).
"""
from __future__ import annotations

from pathlib import Path

OLD = '''    d = iout_pad
    labels.append(("iout", (d[0] + d[2]) / 2, (d[1] + d[3]) / 2, L1LBL))
'''
NEW = '''    d = iout_pad
    y = (d[1] + d[3]) / 2
    layoutlib.rect(top, d[0], y - 0.115, d[2] + 18.5, y + 0.115, L1)
    layoutlib.rect(top, d[2] + 18.5, y - 0.5, d[2] + 19.5, y + 0.5, L1)
    labels.append(("iout", d[2] + 19.0, y, L1LBL))
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
                         + "pex_corners: all\npex_rc_corners: all\n"
                         "pex_rc_knobs: [r_high]\n", encoding="utf-8")
