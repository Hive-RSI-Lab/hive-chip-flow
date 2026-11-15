# sim_compile_error (lint)

iverilog, the compiler the sim gate runs (`-g2012`), refused the design
with an error (a syntax error, a name declared twice, a type it does not
read) that verilator let through. `check_lint.py` runs it at lint so the
design fails here and not at sim. Never allowlist-able.

**Cheapest fix first:** read iverilog's file:line; it is the exact place.
A "syntax error" on legal SystemVerilog means iverilog's parser lacks that
form: rewrite it in plainer SV (see sim_unsupported.md for the usual
rewrites). An "already declared" error is a real bug: remove the
duplicate.

**Trap:** fix the first error only and re-run lint; iverilog's later
errors usually cascade from the first.
