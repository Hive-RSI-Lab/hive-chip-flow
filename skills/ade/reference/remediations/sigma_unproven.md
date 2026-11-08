# sigma_unproven (mc)

A spec measure's Monte Carlo 1-sigma is at or under its `sigma_max`, but
the chi-square upper bound on it (at the yields' per-group confidence,
normal assumed) is above it. The run is too short to prove the sigma.
Routes to `sizing`.

**Cheapest fix first:** if the sample sigma sits well under `sigma_max`,
raise `mc.runs` and run again. The bound tightens about as 1/sqrt(2n). If
it sits close to `sigma_max`, more runs will not be enough: widen the
margin as for sigma_above_spec (a larger matched pair).

**Trap:** lowering `mc.confidence` or raising `sigma_max` is a spec change
for the person.
