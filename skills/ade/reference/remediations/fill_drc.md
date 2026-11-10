# fill_drc (pex_sim)

The tile, filled as the shuttle fills it (the PDK's
`libs.tech/klayout/tech/scripts/fill_all.rb`, run on a copy because the
spec opts in with `pex_fill`), breaks a DRC rule that the drawn tile met.
The finding names the rule. The filled GDS is
`log/pex_sim/fill/<topcell>.filled.gds` and its report is
`log/pex_sim/fill/<topcell>.filled.lyrdb`. Routes to the layout-fixer.

**Cheapest fix first:** open the report on the filled GDS in klayout and
look at where the marker sits:

- a dummy-fill rule (DCF/DPF/DMF) at a gap in the drawn metal: the fill
  went into a slot too narrow for it. Widen the gap past the fill's
  spacing, or close it, in `layout/gen_<block>.py`;
- a drawn-layer rule next to fill: a drawn shape sits closer to the
  tile's edge or to an open area than the fill's keep-out. Move it in the
  generator.

**Trap:** never edit the fill script, strip fill from the copy, or put a
fill-blocking layer over the whole tile to clear it. The shuttle runs that
script unchanged, so its fill is what will be on the wafer. The density
deck is not part of this finding: its rules read the whole die, and the
report lists them under `fill.density_deck` as facts only.
