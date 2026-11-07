# bad_matching (spec_lint)

spec.yaml's `matching` is not a list of groups of the form
`{devices: [a, b, ...], style: common_centroid | interdigitate, tol_um?}`.
Routes to the spec's author.

**Fix:** give each group two or more different instance names from
`netlist/<block>.cir` (e.g. `xmref`), a `style` of `common_centroid` or
`interdigitate`, and, if you set it, `tol_um` as a number above 0 (the
centroid offset allowed, default 0.05 um). A device may be in one group
only. No other key.
