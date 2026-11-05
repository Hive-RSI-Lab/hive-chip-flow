# macro_lib_stale (top_harden)

`macro_lib/<cell>.report.json` was characterised on another netlist than the one
the macro has now: the extraction (`macro_timing.extracted`) or the sized
schematic changed, one appeared or went away, or a file in
`macro_timing.includes` changed. The libs would time the old macro. No harden
ran.

**Fix:** run the `macro_lib` gate on the current netlist, then `top_harden`
again. Do not copy or edit the libs by hand.
