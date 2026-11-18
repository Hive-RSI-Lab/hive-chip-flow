# reset_dependent (reset_indep)

A visible test passes on Verilator with every register at zero and fails
with every register at a random power-up value (`+verilator+rand+reset+2`,
the init seeds named in the finding). Icarus starts each reg at x and an
`if (x)` takes the else branch, so `sim` can pass while a real chip, which
powers up at 0 or 1, sticks. Routes to `rtl`.

**Cheapest fix first:** find the register the reset branch never assigns
(a reg with no `if (rst)` arm, or one assigned only in a branch the reset
skips) and reset it. A register that holds data and genuinely needs no
reset is fine only if no control decision reads it before it is written.

**Replay:** `bin/eda python engine/scripts/check_reset_indep.py
--workspace <ws>` with the finding's `--seed`; the per-run results are in
`log/reset_indep_build/results_seed<N>.xml`.

**Trap:** do not change the bench to hide it (an extra reset cycle, a
wait until outputs settle). The test is right: the design's behaviour after
reset must not depend on its power-up state.
