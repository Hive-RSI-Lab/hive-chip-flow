"""Fault: an offset sigma the sizing does not meet (gates.yaml's mc row) -
tightens vin_offset's sigma_max in spec.yaml from 20 mV to 10 mV and
leaves its +-60 mV per-sample bound alone. This sizing's input offset has
a 1-sigma of about 12.5 mV at tt and 12.0 mV at ff (seeds 1-144, the real
PDK's mismatch), so every sample still lands inside +-60 mV - a 100% yield
- and sim_tt/sim_pvt, at zero mismatch, see an offset under 1 mV. Only the
mc gate's sigma check sees it: sigma_above_spec."""
from pathlib import Path


def plant(ws: Path) -> None:
    s = ws / "spec" / "spec.yaml"
    text = s.read_text(encoding="utf-8")
    old = "sigma_max: 0.02"
    assert text.count(old) == 1, "comparator spec.yaml's sigma_max moved"
    s.write_text(text.replace(old, "sigma_max: 0.01"), encoding="utf-8")
