"""Fault: a macro input left unconnected (gates.yaml's msde top_erc row).
Runs top_harden on the untouched design, then opens the analog macro's
osc_en pin in the powered netlist (`.osc_en()`): a gate inside the macro
that nothing drives, which CVC must report as floating. Refuses when the pin
is not there to open, so a no-op edit can never pass as the fault."""
import re
from pathlib import Path

INSTANCE, PIN = "u_analog", "osc_en"


def plant(ws: Path) -> None:
    import check_top_harden
    payload, _out = check_top_harden.run(["--workspace", str(ws)])
    if payload.get("status") != "pass":
        raise RuntimeError(f"plant_top_erc.py: top_harden did not pass on "
                           f"the untouched design: {payload.get('violations')}")
    pnl = next((ws / "top" / "harden" / "runs" / "run" / "final" / "pnl")
               .glob("*.pnl.v"))
    text = pnl.read_text(encoding="utf-8")
    m = re.search(rf"\b{INSTANCE}\s*\((.*?)\);", text, re.S)
    if not m:
        raise RuntimeError(f"plant_top_erc.py: no instance {INSTANCE} in {pnl}")
    body, n = re.subn(rf"\.{PIN}\s*\([^)]*\)", f".{PIN}()", m.group(1))
    if n != 1:
        raise RuntimeError(f"plant_top_erc.py: {INSTANCE} has no connected "
                           f".{PIN}(...)")
    pnl.write_text(text[:m.start(1)] + body + text[m.end(1):],
                   encoding="utf-8")
