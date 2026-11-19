# expr_not_covered (cover)

Info severity: a single condition term that never decided its expression
(label `(<term>==<value>) => <result>`). It is listed alongside the
`expr_coverage_below_threshold` rollup, which is what fails the gate. See
that file for the fix. To exclude it, use this label in
`rtl/cover_exclude.yaml`.
