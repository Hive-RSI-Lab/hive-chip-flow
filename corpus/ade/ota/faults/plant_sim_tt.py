"""Fault: the Miller compensation cap removed (issue #100). With no xcc
(xrz, its nulling resistor, is left hanging off out) the second stage's
pole is no longer split from the first's, so the unity-gain loop has almost
no phase margin left and pm_deg falls far below its 60 degree bound at
typical."""
from pathlib import Path

OLD = "xcc  d2    dz    cap_mim_2f0_m4m5_noshield c_width=cc_w c_length=cc_w\n"


def plant(ws: Path) -> None:
    p = ws / "netlist" / "ota.cir"
    text = p.read_text(encoding="utf-8")
    if OLD not in text:
        raise RuntimeError(f"{p}: xcc's device line not found as expected")
    p.write_text(text.replace(OLD, "* xcc removed (planted fault)\n"),
                 encoding="utf-8")
