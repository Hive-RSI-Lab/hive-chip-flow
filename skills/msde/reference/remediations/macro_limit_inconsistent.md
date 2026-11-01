# macro_limit_inconsistent (macro_lib)

A warning: at its own `max_capacitance` the output misses its own
`max_transition`, so one of the analog spec's two limits does not hold. STA checks
each against the real net, and only flags this once a net gets that heavy.

**Fix:** lower `max_capacitance_pf` to what the driver can do within
`max_transition_ns`, or strengthen the driver through `analog/`'s own router. Do
not raise `max_transition_ns` past what the digital side's flops are characterised
for.
