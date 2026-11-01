# generated_clock_unreachable (timing)

OpenSTA's `check_setup -generated_clocks` found a generated clock with no
path from its `-source` (master) to the pin it is defined on, so its
latency and waveform are meaningless. Routes to `harden`.

**Cheapest fix first:** the SDC keys the clock on a pin or a `-source`
that no longer connects (a renamed net, a removed buffer, the wrong
master port). Point `-source` at the master clock's port and define the
clock on the output pin of the cell that divides it.
