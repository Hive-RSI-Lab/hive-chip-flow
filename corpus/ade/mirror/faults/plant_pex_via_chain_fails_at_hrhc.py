"""plant_pex_via_chain_fails_at_hrhc.py - ade/mirror `pex_sim` fault the vias
decide, which one R factor misses: xmout's drain reaches the iout pin through a
serial chain of 84 via1, folded into six rows after a short line. DRC and LVS
stay clean. On the wires as extracted the output pole is 510.7 MHz at ss, the
lowest PVT corner, over its 500 MHz floor. With every R x1.156 (the r_high knob)
it is 508.4. At hrhc @ ss it is 492.1: magic's hrhc moves a via from 4.5 ohm to
15, x3.33, where r_high moves it x1.156. lrlc @ ff (626.2), hrlc @ sf (521.1)
and lrhc @ fs (584.6) pass. A plain 36um line with nearly the same nominal pole
(ss 511.7, plant_pex_line_fails_at_c_high.py) passes hrhc at 503.9.
"""
from __future__ import annotations

from pathlib import Path

OLD = '''    d = out["d"]
    labels.append(("iout", (d[0] + d[2]) / 2, (d[1] + d[3]) / 2, L1LBL))
'''
NEW = '''    d = out["d"]
    y = (d[1] + d[3]) / 2
    V1 = layoutlib.GF180_LAYER["via1"]
    L2 = layoutlib.GF180_LAYER["metal2"]
    x0 = d[2] + 6.0
    layoutlib.rect(top, d[0], y - 0.13, x0, y + 0.13, L1)
    vias = [(round(x0 + 0.66 * (c if r % 2 == 0 else 13 - c), 3),
             round(y + 0.62 * r, 3))
            for r in range(6) for c in range(14)]
    for x, v in vias:
        layoutlib.rect(top, x - 0.13, v - 0.13, x + 0.13, v + 0.13, V1)
    layoutlib.rect(top, x0 - 0.19, y - 0.13, x0 + 0.19, y + 0.13, L1)
    for i, (a, b) in enumerate(zip(vias, vias[1:])):
        layer, half = (L2, 0.14) if i % 2 == 0 else (L1, 0.13)
        xs, ys = sorted((a[0], b[0])), sorted((a[1], b[1]))
        hx, hy = (0.19, half) if a[1] == b[1] else (half, 0.19)
        layoutlib.rect(top, xs[0] - hx, ys[0] - hy, xs[1] + hx, ys[1] + hy, layer)
    x, v = vias[-1]
    layoutlib.rect(top, x - 0.19, v - 0.13, x + 0.19, v + 0.6, L1)
    layoutlib.rect(top, x - 0.5, v + 0.5, x + 0.5, v + 1.5, L1)
    labels.append(("iout", x, v + 1.0, L1LBL))
'''


def plant(ws) -> None:
    gen_path = Path(ws) / "layout" / "gen_mirror.py"
    text = gen_path.read_text(encoding="utf-8")
    if OLD not in text:
        raise RuntimeError(
            f"{gen_path}: the iout label lines were not found as expected - "
            "gen_mirror.py's own source shape changed")
    gen_path.write_text(text.replace(OLD, NEW), encoding="utf-8")
    spec = Path(ws) / "spec" / "spec.yaml"
    spec.write_text(spec.read_text(encoding="utf-8")
                    + "pex_corners: all\npex_rc_corners: all\n"
                    "pex_rc_knobs: [r_high]\n",
                    encoding="utf-8")
