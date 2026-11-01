# bad_pex_corners (spec_lint)

spec.yaml's `pex_corners` is not `"all"` or a non-empty list of corner names,
or it names a corner no sweep of this spec runs. Routes to the spec's author.

**Fix:** name corners from the spec's own `corners` (a grid's are
`ss_125c_vm10`, not `ss`), or delete the field: absent means typical, slow/hot
and sim_pvt's worst corner per measure.
