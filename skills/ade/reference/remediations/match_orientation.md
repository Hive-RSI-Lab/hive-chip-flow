# match_orientation (layout_match)

The units of a `matching` group are not all drawn the same way round. The
finding gives each device's mix: the gate axis (`x` for a vertical gate
stripe, `y` for a horizontal one) and `+` for source on the left/bottom,
`-` for drain first. A unit rotated 90 degrees sees a different stress
and implant angle, and a unit mirrored source-for-drain sees a different
implant shadow, so its current differs from its partners'. Each device
must have the same mix as every other device in the group. Routes to
`layout` (the layout-fixer).

**Fix:** in `layout/gen_<block>.py`, place every unit of the group with
the same rotation and the same source/drain order, or pair them so each
device has the same share of each orientation (one `+` and one `-` each).
The finding's `units` list names every unit's position and orientation.

**Trap:** don't swap a unit's source and drain nets in the netlist to
make the order line up: that changes the circuit, and `lvs` checks it.
