"""plant_match_no_dummies.py - ade/mirror `layout_match` fault (#118): the
row drawn B A B, its two end dummies dropped (and from the schematic, so
LVS still matches). The centroids still coincide; only the two end units,
xmout's, have no equal neighbour outside them.
"""
from __future__ import annotations

import re
from pathlib import Path

OLD = 'ROW = "DBABD"\n'
NEW = 'ROW = "BAB"\n'


def plant(ws) -> None:
    gen_path = Path(ws) / "layout" / "gen_mirror.py"
    text = gen_path.read_text(encoding="utf-8")
    if OLD not in text:
        raise RuntimeError(f"{gen_path}: {OLD.strip()} was not found as "
                           "expected - gen_mirror.py's own source shape changed")
    gen_path.write_text(text.replace(OLD, NEW), encoding="utf-8")
    cir = Path(ws) / "netlist" / "mirror.cir"
    net = cir.read_text(encoding="utf-8")
    new, n = re.subn(r"^xmdum\d .*\n", "", net, flags=re.M)
    if n != 2:
        raise RuntimeError(f"{cir}: expected two xmdum dummies, found {n}")
    cir.write_text(new, encoding="utf-8")
