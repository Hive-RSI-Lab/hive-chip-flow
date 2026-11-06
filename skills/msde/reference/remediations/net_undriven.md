# net_undriven (harden)

A signal net in the final ODB has no driver: no instance output and no input
port. Whatever reads it sees an undefined value. The finding names each net.
Routes to `harden`.

**Cheapest fix first:** find the net in the RTL, or in `final/nl` if the name
is a tool's. It is usually a typo, a dropped connection or a port left open on
an instance. Connect it and re-harden. `floating_nets_waived` does not excuse
this finding.
