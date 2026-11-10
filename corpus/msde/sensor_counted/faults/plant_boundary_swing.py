"""Fault: the output stage's pfet sourced from an internal 2.0 V rail, so
osc_out never reaches vih (gates.yaml's named fault for `boundary`)."""
from pathlib import Path


def plant(ws: Path) -> None:
    path = next((ws / "analog" / "netlist").glob("*.cir"))
    text = path.read_text(encoding="utf-8")
    old = "xmpd osc_out nc vdd vdd pfet_03v3"
    if old not in text:
        raise ValueError(f"{path}: no output pfet to fault")
    text = text.replace(old, "vlow2 low2 vss dc 2.0\nxmpd osc_out nc low2 vdd pfet_03v3")
    path.write_text(text, encoding="utf-8")
