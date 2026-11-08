# bad_feedback (spec_lint)

spec.yaml's `feedback` key is present but is not `true` or `false`.

**Cheapest fix first:** write `feedback: true` for a block that closes a
loop (an amplifier in feedback, an LDO, a PLL) and `feedback: false` for
one that does not.
