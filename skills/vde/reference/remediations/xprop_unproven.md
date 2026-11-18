# xprop_unproven (xprop)

SymbiYosys ended UNKNOWN or TIMEOUT: no counterexample within the depth,
but the induction did not close either, so nothing is proven. Routes to
`formal`.

**Cheapest fix first:** raise spec `formal.depth` past the longest path from
reset to a settled state (a counter's period, a FIFO's depth), or raise
`formal.timeout_s` if the run hit the time limit. The gate keeps the depth at
least `reset_cycles + 2`.

**Trap:** do not add `formal.x_init_ok` or `sim.x_ok` entries to make the
proof close: those are for X that is real and harmless, each with a reason,
and no waiver hides a real X.

**Replay:** `bin/eda python engine/scripts/check_xprop.py --workspace <ws>
--timeout <s>`; the log is `log/xprop/sby.log`.
