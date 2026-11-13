# junction_over_breakdown (pex_sim)

The stress stage of `pex_sim` (see `device_overstress.md`) read a drain,
source or body pair past the BVDSS minimum in
`engine/reference/stress_limits.yaml`. The finding names the device, the pair
and the corner. Fix the circuit: the DRM gives no junction allowance, and
sections 14.1.4.1 and 14.1.4.2 do not cover this case with a number.

Junction pairs above 3.63 V but under breakdown do not fail. They are listed
under `stress.junctions_over_reference` with peak and time.
