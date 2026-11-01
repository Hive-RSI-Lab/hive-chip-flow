# mismatch_measure_not_at_tt (spec_lint)

A matching-sensitive measure's own `corners` list leaves out tt (by any of
its names), but the mc gate runs at tt only, so Monte Carlo would never
score it.

**Cheapest fix first:** add `tt` to the measure's `corners` list (or drop
the list: `default` includes tt).
