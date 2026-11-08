# stability_not_all_corners (spec_lint)

The `pm_deg` measure of a `feedback: true` block names its own `corners`
list. A phase margin moves with every process, temperature and supply
corner (the second-stage gm and the output pole both do), so the bound
holds at every corner the spec sweeps.

**Cheapest fix first:** drop the measure's `corners` key (it means
`default`) or set it to `all`.

**Trap:** scoping the margin to the corner that passes is a waiver in
disguise. If ss fails, resize: more compensation cap or more second-stage
current, not a narrower measure.
