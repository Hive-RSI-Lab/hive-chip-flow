"""Fault: Monte Carlo at typical passes, Monte Carlo at the worst PVT
corner misses (gates.yaml's mc row) - takes the core nfet pair xm1/xm2
back from m=4 to m=1. That is a device fault, not a bound fault: the
pair's offset is what the cell's R2/R1 gain multiplies into vref, and at
one unit the vref sigma grows from about 6 mV to about 9.6 mV. Nominal
sim_tt and sim_pvt still pass (vref 1.18011-1.19217 V inside [1.16,
1.21]), and so does Monte Carlo at tt: 345 of the default 348 samples,
a 95%-confidence lower bound just over 95%. But sim_pvt's worst corner
for vref is ff (-40 C, +10% supply; nominal 1.19217 V, 1.9 sigma under
the max), where 13 of 348 samples go over the max: a yield whose lower
bound is near 92%, under spec.yaml's 95%. A gate that ran Monte Carlo
at tt only would pass this block."""
from pathlib import Path


def plant(ws: Path) -> None:
    p = ws / "netlist" / "bandgap.cir"
    text = p.read_text(encoding="utf-8")
    for dev in ("xm1  nb nb e1", "xm2  pb nb s2"):
        old = f"{dev} vss nfet_03v3 w=20e-6 l=2e-6 m=4"
        assert old in text, f"bandgap.cir's {dev.split()[0]} moved"
        text = text.replace(old, old[:-len(" m=4")])
    p.write_text(text, encoding="utf-8")
