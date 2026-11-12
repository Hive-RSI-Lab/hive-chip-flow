# cosim_corner_not_applied (cosim)

A corner run's measures do not say that the bench applied the corner it
was given. A bench that ignored `cosimlib.corner_setup()` runs typical
again, and scoring that at every corner would prove nothing.

**Cheapest fix first:** the bench must read `cosimlib.corner_setup()`, put
its `lines` into the deck (the cell's `.include` is among them, so
`analog_netlist` must be a wrapper that instantiates the cell and does not
hold it) and add `cosimlib.corner_evidence(setup)` to the measures it
writes.

**Trap:** never write the corner name into the measures by hand. The
evidence has to come from the setup the run actually received.
