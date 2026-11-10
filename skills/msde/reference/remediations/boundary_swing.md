# boundary_swing (boundary)

An a2d pin's high level is below vih, or its low level above vil, at the named
corner: the digital input will not read it reliably.

**Cheapest fix first:** check which supply feeds the output stage. It must be
the level's supply, with a final inverter that reaches both rails.

**Trap:** the limit comes from the level table. Do not move it for one block.
