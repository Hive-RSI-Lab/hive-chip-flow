# match_unit_size (layout_match)

The units of a `matching` group are not one size: the extraction gives
two or more W/L pairs among them. The finding lists the sizes. A ratio
built from units of different sizes no longer cancels the edge and
short-channel effects, so it is not the ratio the netlist asks for.
Routes to `layout` (the layout-fixer).

**Fix:** in `layout/gen_<block>.py`, draw every unit of every device in
the group from one unit cell, and set each device's size by its number of
units (`m=`/finger count in `netlist/<block>.cir`), never by a wider unit.
If the netlist itself gives the devices different L, the group is not a
matched group: take it to the spec's author.

**Trap:** never drop the odd-sized device from `matching`. A size set by
`sizing/` stays as sizing set it; the layout follows the netlist.
