# macro_clock_buffer_unproven (top_harden)

A macro's clock output stays repairable so CTS can buffer it, so it is proven
by name instead (#191). The deepest netlist the run reached is walked from
the pin, through buffers CTS named for that net or clock. This fires when the
walk meets any other buffer or an `assign`, in that netlist or in synthesis,
or when the pins it ends on are not the pins synthesis put on the net. Routes
to `top_harden`.

**Cheapest fix first:** take the instance names out of the message. A buffer
in the RTL or a wrapper: remove it and let CTS build the tree. A `rebuffer`,
`wire` or `max_cap` buffer: the resizer repaired part of the net, usually
because the clock also drives a data pin, so sample a flop's copy of it there.
Lost or gained pins: compare them with the design's clock sinks, since another
step moved a sink off or onto the net.

**Trap:** there is no opt-out and no list of allowed names to add to. A cell
that isn't a clock tree buffer on this path has no timing or skew proof, and
exact matching is what keeps an unknown one from passing.
