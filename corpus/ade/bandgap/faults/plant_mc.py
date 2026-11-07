"""Fault: a window that typical meets but mismatch doesn't (gates.yaml's mc
row) - narrows vref to [1.182, 1.194] V (about +-6 mV around typical) in both
spec.yaml and tb/bandgap_tb.bounds.json. Typical (1.18814 V) still passes
sim_tt, but this design's own mismatch spread at seeds 1-20 (1.1688 to
1.2072 V, sigma 9.7 mV, measured with the real PDK) puts 9 of the 20 vref
samples outside it, and the 20 tc samples still pass: yield 77.5%, below
spec.yaml's 95%. Only Monte Carlo sees it."""
import json
from pathlib import Path

LO, HI = 1.182, 1.194


def plant(ws: Path) -> None:
    p = ws / "tb" / "bandgap_tb.bounds.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    for entry in data:
        if entry["measure"] == "vref":
            entry["min"], entry["max"] = LO, HI
    p.write_text(json.dumps(data), encoding="utf-8")
    s = ws / "spec" / "spec.yaml"
    text = s.read_text(encoding="utf-8")
    old = "bounds: {min: 1.16, max: 1.21}"
    assert old in text, "bandgap spec.yaml's vref bounds moved"
    s.write_text(text.replace(old, f"bounds: {{min: {LO:g}, max: {HI:g}}}"),
                 encoding="utf-8")
