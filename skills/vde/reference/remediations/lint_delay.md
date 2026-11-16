# lint_delay (lint)

A `#delay` in the code synthesis sees (`q <= #1 d;`, `assign #2 y = a;`).
Synthesis ignores it, so the sim's timing is not the netlist's, and a
design that only works because of the delay fails after synthesis.
**Fix:** delete the delay; timing comes from the clock and from STA. A
delay a sim model really needs goes under `ifdef SIM`.
