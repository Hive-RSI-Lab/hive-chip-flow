"""Fault: an offset sigma the sizing does not meet (gates.yaml's mc row) -
tightens vin_offset's sigma_max in spec.yaml from 20 mV to 10 mV and
leaves its +-60 mV per-sample bound alone. This sizing's input offset has
a 1-sigma of about 12.5 mV at tt and 12.0 mV at ff (seeds 1-144, the real
PDK's mismatch), so every sample still lands inside +-60 mV - a 100% yield
- and sim_tt/sim_pvt, at zero mismatch, see an offset under 1 mV. Only the
mc gate's sigma check sees it: sigma_above_spec.

It also pins mc.runs to RUNS. sigma_above_spec is the sample 1-sigma over
sigma_max - no confidence bound, so no run count the yield wants - and the
block's default count (several hundred per group) put this one fault at
over ten minutes of the corpus step's budget. Seeds 1-48 put the sample
1-sigma at 12.0 mV at tt and 11.5 mV at ff, both well over 10 mV; the
short run also adds a yield_unproven finding, which the expect does not
name."""
from pathlib import Path

RUNS = 48


def plant(ws: Path) -> None:
    s = ws / "spec" / "spec.yaml"
    text = s.read_text(encoding="utf-8")
    old = "sigma_max: 0.02"
    assert text.count(old) == 1, "comparator spec.yaml's sigma_max moved"
    text = text.replace(old, "sigma_max: 0.01")
    mc = "mc: {enabled: true, yield_min: 0.95, seed: 1}"
    assert text.count(mc) == 1, "comparator spec.yaml's mc line moved"
    s.write_text(text.replace(
        mc, f"mc: {{enabled: true, yield_min: 0.95, seed: 1, runs: {RUNS}}}"),
        encoding="utf-8")
