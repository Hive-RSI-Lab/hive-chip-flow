# verilator_baseline_failed (reset_indep)

A visible test that `sim` runs on Icarus fails on Verilator even with every
register at zero, so reset_indep cannot compare it and leaves it out. The
gate still passes on this warning; the test simply goes unchecked for
reset independence. Routes to `testbench`.

**Usual causes:** the bench samples an output in the same delta as the
clock edge (Icarus and Verilator order these differently; sample after
`ReadOnly()` or on the falling edge), or the RTL uses a construct the two
simulators read differently (an `initial` block, a `#delay`, x on an
input). Fix the bench's timing first; an RTL construct that only one
simulator accepts is the rtl-writer's.
