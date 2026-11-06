# net_unloaded (harden)

A signal net in the final ODB has no load: no instance input and no output
port, and an antenna diode does not count. The finding names each net. A TT
wrapper input (`clk`, `rst_n`, `ena`, `ui_in`, `uio_in`) is exempt, since the
tile has those pins whether the design reads them or not. Routes to `harden`.

**Cheapest fix first:** find the net in the RTL, or in `final/nl` if the name
is a tool's. It is usually a typo, a dropped connection or logic whose result
nothing reads. Connect it or remove its driver, then re-harden. If it really is
meant to have no load, waive it by name with a reason:

    harden_signoff:
      floating_nets_waived:
        some_net: why no load is correct here
