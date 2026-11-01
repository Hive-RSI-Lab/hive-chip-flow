# unclocked_register (timing)

OpenSTA's `check_setup` found register/latch clock pins no clock reaches,
while the rest of the design has a finite slack - so those registers are
not timed at all. Routes to `harden`.

**Cheapest fix first:** the usual cause is a divided or gated clock (a
flop's Q, a TSPC prescaler output) with no `create_generated_clock` in
`harden/constraints.sdc`, or one keyed on a net/pin name the netlist no
longer has (a yosys rename moved the alias). The finding lists the
pins: trace each back to the flop that drives it and declare the
generated clock on that driver's output pin.

**Trap:** do not silence it with `set_false_path` or by deleting the
check - an unclocked bank is exactly the one whose timing nobody has
seen.
