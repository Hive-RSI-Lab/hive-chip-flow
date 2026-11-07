"""plant_erc_floating_gate.py - ade/mirror `erc` fault: the iref_node bus
stops short of xmout's gate pad, so nothing drives xmout's gate (a missing
wire). xmref's diode connection is untouched. CVC reports xmout's gate as a
floating (HI-Z) input and the erc gate names the device.
"""
from __future__ import annotations

from pathlib import Path

OLD = "layoutlib.rect(top, g0[0], g0[1], g1[2], g0[3], L1)"
# end the bus halfway across the gap, past the iref_node label
NEW = ("layoutlib.rect(top, g0[0], g0[1], "
       "(ref[\"d\"][2] + g1[0]) / 2 + 0.1, g0[3], L1)")


def plant(ws) -> None:
    path = Path(ws) / "layout" / "gen_mirror.py"
    text = path.read_text(encoding="utf-8")
    if OLD not in text:
        raise RuntimeError(f"{path}: iref_node bus line not found as expected "
                           "- gen_mirror.py's own source shape changed")
    path.write_text(text.replace(OLD, NEW), encoding="utf-8")
