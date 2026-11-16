# lint_incomplete_sensitivity (lint)

An `always @(a ...)` block reads a signal its sensitivity list leaves out.
Sim re-runs the block only when a listed signal changes, so the output
holds a stale value; synthesis ignores the list and builds plain
combinational logic. Sim and netlist then disagree, and only gate-level
sim notices. **Fix:** write `always @*` (or `always_comb` in SV). Do not
add the missing names by hand: the next edit will miss one again.
