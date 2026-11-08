# ruling_sigma_unmet (mc)

A `below_spread` ruling in `spec/mutant_rulings.yaml` claims a relative
Monte Carlo 1-sigma for its measure larger than the mc gate measures at tt
(its upper confidence bound, the largest across benches). bench_strength
excused that mutant against a spread the block does not have.

**Cheapest fix first:** set the ruling's `sigma` to what the report's
`ruling_sigmas` entry measures (`mc_rel_sigma`) and run bench_strength
again. If the mutant now shows outside the spread, strengthen the bench so
it kills it.

**Trap:** do not raise `mc.runs` hoping the measured sigma grows; more
runs only tighten its bound.
