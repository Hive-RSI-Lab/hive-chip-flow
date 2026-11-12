# device_overstress (top_stress)

A standard cell the top uses pushed a gate pair of one of its devices past
the allowance in a transistor-level run of the cell alone. The finding names
the cell, the device, the pair, the corner, the bench (light or heavy load),
how many runs it fails in and how many instances of the cell the top holds.
The cell is the PDK's, so the fix is in which cells the top uses and what
drives them, not in the cell.

The limit is the device class's in `engine/reference/stress_limits.yaml`:
3.63 V for a 3.3 V device, 5.5 V for a 5 V (`05v0`) device, from GF180MCU
DRM 14.1.2 and 14.1.4.2. The share, band and one-off rules are pex_sim's, in
the ade reference `device_overstress.md`.

**Cheapest fix first:** read the report's `cells` entry for the cell. If it
fails only on the heavy bench, the load is what drives it: a net in the top
with more capacitance than the cell is sized for. Use a stronger drive
variant on that net, or split the load with a buffer, then `top_harden`
again. If it fails on the light bench too, the cell itself overshoots at the
top's edges and has to stay out of the top. The flow has no `dont_use` list
yet, so open an issue naming the cell and the report rather than edit the
PDK's library.

**Trap:** do not lower the supply in `analog/spec/spec.yaml`, slow the clock
or edit the cell's spice to make the run pass. The gate runs at the supply
+10% and the fast corners because that is what the silicon can see.
