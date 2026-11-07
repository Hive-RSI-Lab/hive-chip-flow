# measure_out_of_bounds (pex_sim)

A post-layout measure came out of its bound at the corner the finding names,
against the magic-extracted parasitic netlist. Routes to `layout` (the layout-fixer) -
parasitics the routing itself added (a long, narrow output line's own R
and C, an under-sized via count) are the layout's fault, distinct from
`sim_bound_fail` (a pre-layout schematic miss, which routes to `sizing`).

**Cheapest fix first:** the finding names the measure and its bound; check
`layoutlib.count_parasitics()`'s R/C counts in the gate's own report for
which nets carry the most extracted parasitics. A long minimum-width
routing segment on a sensitive node (the mirror's own `plant_pex_
long_output_line.py` fault shape) is the common case - widen the trace or
shorten the run in `layout/gen_<block>.py`.

A finding naming a knob (`f3db @ ss / c_high (C x1.2, R x1)`) is the same fix:
it passed on the wires as extracted and failed with them scaled, so the route
has too little margin.

**Trap:** `pex_sim` runs a chosen set of corners, on magic's extractor only,
with RC knobs only if the spec opts in, and no foundry RC corners (SKILL.md's
Known limits) - prefer a fix with real margin
over one that only just clears the bound. Widening a bound, or scoping it
away from the failing corner, is a spec decision, not a layout fix.
