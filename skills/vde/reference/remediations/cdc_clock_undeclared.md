# cdc_clock_undeclared (cdc)

A port clocks flops but `clock.domains` in spec.yaml leaves it out, so a data
input is probably being used as a clock. Routes to `rtl`.

**Fix:** sample the port as data in an existing domain, or if it is a clock,
add it to `clock.domains` (a spec edit).
