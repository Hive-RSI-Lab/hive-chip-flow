# r2r_dac - 2-bit R2R ladder DAC

The second rung of `/ade`'s corpus (docs/design.md section 3, "### M8."):
a passive block whose whole design correctness rests on a resistor RATIO,
and the corpus rung this milestone's optimise loop (section 4) is written
against.

## What it does

A 2-bit R2R resistor ladder: `bmsb` (nearest the output through a single R
leg) and `blsb` (one more R+2R stage removed) drive the ladder, and `vout`
reads the resulting divided voltage - measured empirically on this design
(unloaded, no output buffer): MSB alone gives ~2.4V, LSB alone ~0.6V, both
~3.0V, at VDD=3.3V and the correct 2:1 R:2R ratio.

## Interface

`r2r_dac(bmsb, blsb, vout, r_length=..., r2_length=...)` - each leg is this
PDK's own `rm1` poly resistor (`devices: [xrmsb, xr2lsb, xrlsb, xr2msb]`
in spec.yaml, real gf180mcu_fd_pr instantiations netlist_lint checks
against, not a plain ngspice `R` element with a bare numeric value - see
netlist/r2r_dac.cir's own comment for the earlier version of this rung and
why it changed). `r_length`/`r2_length` are the R/2R leg lengths (meters)
at a shared, fixed width; the R:2R ratio the transfer function depends on
is the length ratio (rm1's sheet resistance cancels out of it regardless
of corner), which is also why `corners: default`'s 5-corner sweep passes
identically at every corner - a real, corner-dependent device whose
ratio-based design is meant to reject that dependence, not a claim with
nothing behind it.

## Reference

The bits switch between 0 V and a fixed 3.3 V reference, which does not
follow VDD. The ladder's output is a fixed fraction of that reference and
the bounds are absolute volts, so with bits driven from VDD no sizing could
pass the +/-10% supply corners (0.54 V against the 0.55 V floor on
`vout_lsb` at -10%). The bench models the reference as an ideal source. A
real design has to build it, and its error adds directly to these bounds.

## Requirement, and this rung's own twist

`vout_lsb`/`vout_msb`/`vout_both` (docs/design.md 4's per-measure bounds)
are all inside their bound ONLY at the correct 2:1 ratio. This rung ships
with `sizing/sizing.yaml` (and the netlist's own subckt defaults) set to
the WRONG 1:1 ratio on purpose - `sim_tt` is deliberately NOT in this
rung's `faults/manifest.yaml` (it is expected to fail on the untouched
reference, the same way a harder corpus rung's "row may stay red" until
its own later milestone lands, docs/design.md "### M6."/"### M9.") -
`engine/scripts/optimise.py numeric` against `sizing/sizing.yaml` is this
milestone's own done criterion for reaching every measure inside bounds
from that wrong start.
