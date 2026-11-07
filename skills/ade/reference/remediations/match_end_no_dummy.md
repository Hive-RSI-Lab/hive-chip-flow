# match_end_no_dummy (layout_match)

A unit of a `matching` group has no equal device (same model, W and L,
same axis) on one side within 1.5 unit pitches. The finding gives the
unit's position and the open side. An end unit sees a different
neighbourhood from the middle ones (etch, poly density, well proximity),
so it comes out a little different from its partners and the match is
lost. Routes to `layout` (the layout-fixer).

**Fix:** in `layout/gen_<block>.py`, add a dummy at each open end of the
row (and of each column, for a 2-D array): a device the same size and
orientation as the units, at the same pitch, with gate, source and drain
tied to the rail it sits on (vss for an NMOS row, vdd for a PMOS row; a
resistor dummy's two ends and body on vss). `layoutlib.matched_row`
draws a row with its `dummies` at both ends from the same unit cell, so
they are equal by construction.
`lvs` compares device for device, so `netlist/<block>.cir` needs the same
dummies, every terminal on that rail (`xmdum1 vss vss vss vss nfet_03v3
...`, `xrdum1 vss vss vss ppolyf_u ...`). The netlist is not the layout-fixer's to edit: if its dummies are
missing, name the lines it needs in OPEN for the netlist's author.

**Trap:** a dummy of a different size, or one left floating, is not a
dummy: the check counts only equal devices, and a floating gate fails
`erc`. Never shrink the group in `matching` to leave the end unit out.
