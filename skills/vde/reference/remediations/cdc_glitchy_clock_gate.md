# cdc_glitchy_clock_gate (cdc)

Logic on a clock path with no glitch-free structure. Routes to `rtl`.

**Cheapest fix first:** an ICG cell, or launch the enable straight from a flop
on the opposite edge of the same clock: changing while the clock is low for an
AND gate, while it is high for an OR gate, with no logic between. The
`timing` gate checks the same enable's clock-gating setup and hold.
