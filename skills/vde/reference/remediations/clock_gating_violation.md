# clock_gating_violation (timing)

A clock gate's enable changes while the clock it gates is in its
controlling phase (clock-gating setup or hold), so the gated clock can
glitch. Routes to `harden`.

**Cheapest fix first:** use an integrated clock-gating cell (a latch-based
ICG) rather than a bare AND/OR, or launch the enable from the opposite
clock edge so it settles while the clock is low (AND) or high (OR).
