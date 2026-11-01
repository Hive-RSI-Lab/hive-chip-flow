# macro_clock_undefined (top_harden)

`macro_timing.clocks` says the analog macro sources this clock, and the SDC
creates none on that pin at this corner, so the flops it drives are unclocked.
No harden ran.

**Fix:** add `create_clock -period [macro_clock_period <pin>] [get_pins
u_analog/<pin>]` to `top.sdc`, or to the digital SDC on the crossing port, which
`top_harden` moves onto the pin. If the macro no longer sources it, take it out
of `macro_timing.clocks`.
