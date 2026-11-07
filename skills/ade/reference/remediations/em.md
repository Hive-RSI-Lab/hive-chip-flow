# em_wire_over / em_cut_over (em_ir)

The `em_ir` gate ran the block's operating-point bench
(`<block>_emir_tb.cir`) on magic's extraction at every corner of the sweep,
and a wire or a cut array carries more DC current than the GF180MCU DRM
allows at 125 C (`engine/reference/em_limits.yaml`). The finding names the
net, the corner and the position in um (a wire's midpoint, or a cut
array's box and GDS layer). The extraction and the per-corner benches are
in `log/em_ir/`.

The limits are never edited per block. Fix the layout generator.

- **em_wire_over**: the wire is too narrow for its current. Widen it in
  the generator (the limit is per um of drawn width: metal1-4 0.67 mA/um,
  metal5 1.21 mA/um), or run a parallel strap on another metal and join
  them with enough vias. A rail carrying the whole block's current needs
  the most width where it meets its pin.
- **em_cut_over**: too few cuts in one via or contact array for the
  current crossing it (0.18 mA a cut for contact and via1-4, 0.14 mA for
  a poly contact). Draw more cuts there, or move part of the current to a
  second array. The count is the GDS's own: one drawn cut is one cut.

**Trap:** don't shrink the bench's current to pass. The bench sets the
block's heaviest DC state on purpose (the code, bias and load that draw
the most supply current); a lighter state is a weaker check, not a fix.
