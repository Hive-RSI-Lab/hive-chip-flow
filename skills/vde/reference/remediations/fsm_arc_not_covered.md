# fsm_arc_not_covered (cover)

Info severity: a single state-machine transition that the visible suite
never took (label `<register>::<from>-><to>`). It is listed alongside the
`fsm_coverage_below_threshold` rollup, which is what fails the gate. See
that file for the fix. To exclude it, use this label in
`rtl/cover_exclude.yaml`.
