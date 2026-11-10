# boundary_threshold (boundary)

A d2a pin's switching point is outside the band a digital driver is sure to
cross, or the observed net never crossed vdd/2 during the ramp.

**Cheapest fix first:** check that `observe` is a net that really follows this
pin with the other pins at the `static` levels; then re-ratio the input stage.
