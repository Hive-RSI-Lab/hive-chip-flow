# feedback_undeclared (spec_lint, warning)

The block's `top` name reads as one that closes a loop (`ota`, `opamp`,
`amp`, `ldo`, `regulator`, `pll` as a word of it), but spec.yaml has no
`feedback` key. A warning: it does not fail the gate.

**Cheapest fix first:** say which it is. A block used inside a loop gets
`feedback: true` and a `pm_deg` measure with a `min` bound (see
`feedback_no_stability.md`); an open-loop block (a comparator named
`amp_...`, a buffer that never closes a loop) gets `feedback: false`.

**Trap:** renaming `top` to dodge the name check leaves the loop
unchecked. The loop is still there.
