# yield_unproven (mc)

The Monte Carlo yield at a role (tt, or each bench's worst sim_pvt corner)
meets `mc.yield_min` on its face, but its Clopper-Pearson lower bound at
`mc.confidence` (Bonferroni over every bench and corner group) does not.
The run is too short to prove the yield. Routes to `sizing`.

**Cheapest fix first:** read `runs_needed` on the finding. If it is a
number, the design may be fine: set `mc.runs` to about that (or leave
`runs` out, so the gate sizes it from `yield_min`) and run again. If it is
null, no run count proves it at this failure rate: widen the design's
margin (larger matched devices, more headroom at the worst corner named in
`worst_corners`).

**Trap:** lowering `yield_min` or `confidence` is a spec change for the
person. A worst-corner miss is a real miss even when tt passes.
