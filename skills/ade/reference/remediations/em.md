# em_wire_over / em_cut_over / em_res_body_over / em_res_contact_over (em_ir)

Also their transient forms, `em_<x>_rms_over` and `em_<x>_peak_over`.

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

- **em_res_body_over**: a PDK poly resistor's body carries more than its
  DRM limit per um of drawn width (PRES.8, LRES.8, HRES.11, PL.10: 1 mA/um).
  Widen the resistor and lengthen it in step so its value holds, or split
  it into parallel legs. The width is the instance's `r_width`.
- **em_res_contact_over**: the contact row at one head of a resistor is too
  short for the current through it (0.14 mA a poly contact). Widen the
  head so it takes more contacts; a wider body gives a wider head.

## The transient forms

A block with `<block>_emir_tran_tb.cir` is also held over that bench's
window to `pulsed:` in em_limits.yaml: the mean |I| against the DC limit
(`em_wire_over` etc.), I_rms against it (`_rms_over`), and the peak
against the limit over its duty, at most 10x (`_peak_over`). The finding
says which. An rms or peak finding on a strap that passes on average is a
strap sized for the mean of a current that comes in pulses: widen it for
the pulse. The bench runs the block through whole periods of its heaviest
steady activity, `tstart` past the settling, with its max step at most the
fastest edge / 25.

A transient puts the wells' and resistor bodies' capacitance current on the
substrate. magic joins the substrate to its net at one spot with no drawn
tap, so the gate charges that current, whole, to every drawn substrate tap
(a contact on P+ COMP outside Nwell): a contact finding on a tap means the
tap needs more cuts. A refusal that the GDS draws no substrate tap means
just that: draw one (layoutlib's P+ tap) and strap it to vss.

**Trap:** don't shrink the bench's current to pass. The bench sets the
block's heaviest DC state on purpose (the code, bias and load that draw
the most supply current); a lighter state is a weaker check, not a fix.
