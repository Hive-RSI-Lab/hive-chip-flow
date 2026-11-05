# macro_net_unresolved (top_harden)

A macro's non-clock output is kept out of repair (#191): repair_design
buffered such a net over and over until the job ran out of memory. The
net's name is read from synthesis, never guessed: a probe synthesizes the
top first, and the harden's own synthesis is checked again. This fires
when the named `<instance>/<pin>` has no single net in that netlist: the
pin is tied to a constant or a concatenation, left open, or the instance or
pin is missing. Routes to `top_harden`.

**Cheapest fix first:** open the netlist named in the message and find the
macro instance. Wire each output the digital side reads to one plain wire in
the top wrapper. An output nothing reads should be dropped from the macro's
pins or left unconnected in its spec, not tied.

**Trap:** don't rename the net to make it match. Nothing is matched by
pattern, so a rename changes nothing. The pin needs exactly one net.
