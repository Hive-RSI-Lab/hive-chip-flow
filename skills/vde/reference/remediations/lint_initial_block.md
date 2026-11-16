# lint_initial_block (lint)

An `initial` block in the code synthesis sees. Sim starts the register at
that value; synthesis drops the block, so the real chip powers up with an
unknown value. **Fix:** give the register a reset (`if (!rst_n) q <= 0;`).
A block that is really sim-only (a `$display`, a `$readmemh` for a sim
model) goes under `ifdef SIM` or `ifndef SYNTHESIS`, which lint leaves out.
