# bad_statistical (spec_lint)

A measure's `statistical` is not `mismatch` or `none`, or it says `none`
with no `mc_waiver` reason.

**Cheapest fix first:** `statistical: mismatch` for a measure device
mismatch moves (then the spec needs `mc.enabled: true`); `statistical:
none` plus `mc_waiver: <why mismatch cannot move it>` for a name that only
reads matching-sensitive.
