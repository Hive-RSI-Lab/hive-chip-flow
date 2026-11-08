"""Fault: a window that typical meets but mismatch doesn't - narrows vref
to [1.182, 1.194] V (about +-6 mV around typical) in both spec.yaml and
tb/bandgap_tb.bounds.json. Typical (1.18949 V) still passes sim_tt, but
this design's own mismatch spread at tt (sigma about 6 mV, measured with
the real PDK) puts 110 of the 348 default vref samples outside it while the
tc samples all pass: yield 68%, below spec.yaml's 95% at tt itself - the
plain case. plant_mc_worst.py is the one only the worst-corner run sees."""
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
