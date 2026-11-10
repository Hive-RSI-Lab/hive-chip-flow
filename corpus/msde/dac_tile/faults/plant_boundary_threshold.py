"""Fault: bmsb's switch ratioed so hard its switching point falls below vil:
a digital driver can leave it undecided."""
from pathlib import Path


def plant(ws: Path) -> None:
    path = next((ws / "analog" / "netlist").glob("*.cir"))
    text = path.read_text(encoding="utf-8")
    for old, new in (("xmpm dm bmsb vdd vdd pfet_03v3 w=4e-6",
                      "xmpm dm bmsb vdd vdd pfet_03v3 w=4.5e-7"),
                     ("xmnm dm bmsb vss vss nfet_03v3 w=2e-6",
                      "xmnm dm bmsb vss vss nfet_03v3 w=9e-5")):
        if old not in text:
            raise ValueError(f"{path}: no {old!r} to fault")
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
