# macro_clock_net_unresolved (top_harden)

A macro's clock output stays repairable so CTS can buffer it, so it is proven
by name instead (#191). A probe synthesizes the top and reads the net on the
pin. This fires when that `<instance>/<pin>` has no single net (no harden
runs), when the harden's synthesis or the deepest netlist it reached has
another net on the pin, or when the CTS log does not name the net as a
clock's. Routes to `top_harden`.

**Cheapest fix first:** read which of the three the message names. No single
net: wire the output to one plain wire, as for `macro_net_unresolved`. A
renamed net is usually a top output driven straight from the clock, whose
port buffer takes the pin onto `netNN`: drive that output from a flop or a
gate the clock reaches instead. CTS not naming the net: make the top SDC's
`create_clock` name this pin, in the form `<instance>/<pin>`, and give the
net a sink that uses it as a clock.

**Trap:** don't move the output onto a new wire to make the names agree. The
name is read from the netlist, so what must hold is that one net runs from
the probe to the end of the run. A net CTS doesn't call a clock is data to
repair_design, which is how a macro output ran the job out of memory.
