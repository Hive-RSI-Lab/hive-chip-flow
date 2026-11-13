"""Fault: a top whose standard cells see more than their device class
allows (gates.yaml's msde top_stress row). Runs top_harden on the untouched
design, then declares the analog spec's supply at 5.0 V instead of 3.3 V:
the gate drives every cell at that supply +10% (5.5 V, the 5 V-class
limit), so each edge's overshoot carries a gate past it. Only the spec
moves: the cells, layout and parasitics are the ones that pass at 3.3 V.
Refuses when the spec has no `vdd: 3.3` to raise, so a no-op edit can never
pass as the fault."""
import re
from pathlib import Path

FROM_V, TO_V = "3.3", "5.0"


def plant(ws: Path) -> None:
    import check_top_harden
    payload, _out = check_top_harden.run(["--workspace", str(ws)])
    if payload.get("status") != "pass":
        raise RuntimeError(f"plant_top_stress.py: top_harden did not pass on "
                           f"the untouched design: {payload.get('violations')}")
    spec = ws / "analog" / "spec" / "spec.yaml"
    text = spec.read_text(encoding="utf-8")
    new, n = re.subn(rf"(?m)^(supply:\s*\n\s+vdd:\s*){re.escape(FROM_V)}\s*$",
                     rf"\g<1>{TO_V}", text)
    if n != 1:
        raise RuntimeError(f"plant_top_stress.py: {spec} declares no "
                           f"supply.vdd {FROM_V} to raise")
    spec.write_text(new, encoding="utf-8")
