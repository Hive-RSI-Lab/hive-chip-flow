# ua_pin_not_a_port (spec_lint)

A name in spec.yaml's `ua_pins` is not a port of the top's `.subckt` in
`netlist/*.cir`. sim_run puts the ua pad model on that port of every top
instance in every bench, so a pin that is not a port has nowhere to go.

Check the spelling against the netlist's `.subckt` line. If the port was
renamed, change `ua_pins` (a `spec_edit`) or the netlist to agree. If the
pin really leaves the tile, it has to be a port of the top.

**Trap:** do not drop the name to pass the check. A ua pin missing from the
list runs with no pad at all.
