# survivor_cap_removed (bench_strength)

A capacitor listed in spec.yaml's `devices` was removed from the netlist
(its line commented out), and no measure moved out of bound or past its
declared `sensitivity`. Routes to `testbench`.

**Cheapest fix first:** a bench that cannot tell a missing cap from a
present one measures nothing the cap does. For a compensation cap, add the
stability bench (`skills/ade/templates/stb_tb.cir`) and bound `pm_deg`
at the spec's `min` at every corner: with the Miller cap gone a two-stage
amplifier's phase margin collapses. For a decoupling or filter cap,
measure the ripple, the corner frequency or the step response it shapes.

**Trap:** removing the cap from `devices` makes the gate warn
`device_undeclared` instead, which hides the weakness rather than fixing
it. If the cap really does nothing the block promises, the right change
is a design one: take it out of the netlist.
