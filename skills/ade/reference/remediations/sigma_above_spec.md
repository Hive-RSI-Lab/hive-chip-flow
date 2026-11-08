# sigma_above_spec (mc)

A spec measure with a `sigma_max` (a comparator's input-offset sigma) has a
Monte Carlo sample 1-sigma above it at tt or at a bench's worst sim_pvt
corner (the finding names `bench@corner`). The design misses. Routes to
`sizing`.

**Cheapest fix first:** mismatch sigma falls as 1/sqrt(W*L) of the matched
devices (Pelgrom). Find the pair that sets the measure: for a StrongARM's
offset that is the input pair first, then the latch NMOS. Scale its area by
about (sigma / sigma_max)^2, with margin. The input pair's overdrive also
matters: a lower Vgs - Vt gives less offset per mV of Vt mismatch. A miss
only at a corner wants margin at that corner.

**Trap:** raising `sigma_max` is a spec change for the person, and so is
changing the topology (a preamp, offset storage). More `runs` do not lower
the sigma; they only tighten its bound.
