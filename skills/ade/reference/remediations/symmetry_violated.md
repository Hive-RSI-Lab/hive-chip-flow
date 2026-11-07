# symmetry_violated (pex_sim)

spec.yaml's `symmetric_nets` declares a pair (a differential output, UP/DN)
that must route alike, and the extracted netlist gives the two nets a
different total C (ground plus coupling), pin-to-device R, or C to a named
aggressor, by more than the pair's `max_dc_pct`/`max_dr_pct` of their mean
(and by more than `min_dc_ff`/`min_dr_ohm`, when the spec sets a floor).
The finding gives both values. pex_sim checks this before the bench, from
the extraction alone: a mismatch the bench at one input happens to
tolerate still costs offset, skew or charge-pump current mismatch.
Routes to `layout` (the layout-fixer).

**Fix:** in `layout/gen_<block>.py`, draw the two nets as mirror images:
the same length, width, layers and number of vias on each, and the same
neighbours. Where they must cross, give each net its own crossing (one
over the other's tracks, one under), not one net straight across and the
other around it - the extra via pair alone is several ohms. The report's
`symmetry` block lists each pair's C, R and per-aggressor values, so you
can see which quantity moved and on which net.

**Trap:** never widen `max_*_pct`, add a floor or drop the pair from
`symmetric_nets` to clear this - that is the spec's author's call, not the
layout's, and a floor large enough to hide a real route difference
loosens the gate. Never move a pin label to shorten the measured R: the
label is the pin, and R is read from it.
