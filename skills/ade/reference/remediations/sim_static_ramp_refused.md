# sim_static_ramp_refused (sim_tt, sim_pvt, bench_strength, mc)

A bench uses `{{RAMP_FILE}}`, and its sweep could not be scored through
`convlib.static_ramp`: there is no `* static_ramp_codes: N` line, ngspice wrote
no file, or the file is not codes 0..N-1 in order with one finite level each.

**Cheapest fix first:** read the message and the deck under `log/sim/`. Sweep
the integer code from 0 to N-1 in steps of 1, then `wrdata {{RAMP_FILE}} <vector>`
with exactly one vector, after the sweep and before any `setplot` or `alter`.

**Trap:** never lower `static_ramp_codes` to match a short sweep. A sweep that
stops early never exercises the codes it skipped, and it is the sweep to fix.
