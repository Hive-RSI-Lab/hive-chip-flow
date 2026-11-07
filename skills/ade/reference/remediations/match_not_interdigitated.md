# match_not_interdigitated (layout_match)

spec.yaml's `matching` declares an `interdigitate` group, and along the
row one device has a longer run of adjacent units than the ratio needs
(for 1:1, more than one in a row; for 1:3, more than three). The finding
gives the row as letters (A = the group's first device) and the longest
run. Routes to `layout` (the layout-fixer).

**Fix:** in `layout/gen_<block>.py`, alternate the units: ABAB for 1:1,
ABBBABBB-style for 1:3, so each device's units are spread across the
whole row. `layoutlib.matched_row(..., pattern="interdigitate")`
spreads the units by ratio and returns the slots and pads to wire.

**Trap:** don't change the group's `style` to `common_centroid` to clear
this: that is the spec's author's call.
