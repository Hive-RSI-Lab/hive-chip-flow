"""Fault: the output stage at an eighth of its width, driving the 2 pF the
interface now declares: osc_out's edge is over the library's limit."""
from pathlib import Path

import yaml


def plant(ws: Path) -> None:
    path = next((ws / "analog" / "netlist").glob("*.cir"))
    text = path.read_text(encoding="utf-8")
    for old, new in (("xmpd osc_out nc vdd vdd pfet_03v3 w=1.2e-5",
                      "xmpd osc_out nc vdd vdd pfet_03v3 w=1.5e-6"),
                     ("xmnd osc_out nc vss vss nfet_03v3 w=6e-6",
                      "xmnd osc_out nc vss vss nfet_03v3 w=7.5e-7")):
        if old not in text:
            raise ValueError(f"{path}: no {old!r} to fault")
        text = text.replace(old, new)
    path.write_text(text, encoding="utf-8")
    interface = ws / "interface.yaml"
    data = yaml.safe_load(interface.read_text(encoding="utf-8"))
    for sig in data["signals"]:
        if sig["name"] == "osc_out":
            sig["load"] = {"cap_ff": 2000}
            break
    else:
        raise ValueError(f"{interface}: no osc_out to fault")
    interface.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
