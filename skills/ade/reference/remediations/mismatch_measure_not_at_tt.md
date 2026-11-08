# mismatch_measure_not_at_tt (spec_lint)

A matching-sensitive measure's own `corners` list leaves out tt (by any of
its names). tt is the one corner the mc gate always runs; its second run
is at each bench's worst sim_pvt corner by margin, which need not be one of
the measure's own, so Monte Carlo might never score it.

**Cheapest fix first:** add `tt` to the measure's `corners` list (or drop
the list: `default` includes tt).
