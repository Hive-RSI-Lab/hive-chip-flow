# bad_symmetric_nets (spec_lint)

spec.yaml's `symmetric_nets` is not a list of pairs of the form
`{nets: [a, b], max_dc_pct?, max_dr_pct?, min_dc_ff?, min_dr_ohm?,
aggressors?}`. Routes to the spec's author.

**Fix:** give each pair two different net names of the block's netlist, at
least one of `max_dc_pct`/`max_dr_pct` (a percentage of the pair's mean,
0 or more), floors only as numbers 0 or more (fF and ohm), and
`aggressors` only as a list of other nets' names alongside `max_dc_pct`,
which bounds their C too. No other key.
