"""plant_pex_line_slow_corner_only.py - ade/mirror `pex_sim` fault that only a
corner sweep can see: the iout pin moves to the far end of a 50um run of
0.23um metal1, a tenth of plant_pex_long_output_line.py's. DRC and LVS stay
clean. The output pole stays inside its bound at typical (513 MHz against
the 500 MHz floor) and falls under it at slow and hot (ss 476 MHz, sf 486
MHz), so a typical-only pex_sim passes this layout.
"""
from __future__ import annotations

from pathlib import Path

OLD = '''    d = out["d"]
    labels.append(("iout", (d[0] + d[2]) / 2, (d[1] + d[3]) / 2, L1LBL))
'''
NEW = '''    d = out["d"]
    y = (d[1] + d[3]) / 2
    layoutlib.rect(top, d[0], y - 0.115, d[2] + 50.0, y + 0.115, L1)
    layoutlib.rect(top, d[2] + 50.0, y - 0.5, d[2] + 51.0, y + 0.5, L1)
    labels.append(("iout", d[2] + 50.5, y, L1LBL))
'''


def plant(ws) -> None:
    gen_path = Path(ws) / "layout" / "gen_mirror.py"
    text = gen_path.read_text(encoding="utf-8")
    if OLD not in text:
        raise RuntimeError(
            f"{gen_path}: the iout label lines were not found as expected - "
            "gen_mirror.py's own source shape changed")
    gen_path.write_text(text.replace(OLD, NEW), encoding="utf-8")
