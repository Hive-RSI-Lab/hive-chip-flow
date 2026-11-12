# junction_over_breakdown (top_stress)

A standard cell the top uses put a drain, source or body pair of one of its
devices past the BVDSS minimum in `engine/reference/stress_limits.yaml`, in a
transistor-level run of the cell alone. The finding names the cell, the
device, the pair, the corner and the bench. The DRM gives no junction
allowance, so any excess fails.

Fix it as for [device_overstress.md](device_overstress.md): a stronger drive
or a buffer for a heavy net, or keep the cell out of the top. Junction pairs
over the class limit but under breakdown do not fail; they are listed under
`stress.junctions_over_reference` with peak and time.
