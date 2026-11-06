# floating_net (harden)

A signal net has no driver or no load, by the name in the log of the step that
counted it. Power nets (`VPWR`, `VGND`) are counted there on every TT harden
and do not fail. Routes to `harden`.

**Cheapest fix first:** read the named net in the RTL. It is usually a typo, a
dropped connection or an output nothing reads. If it really is meant to float,
waive it by name with a reason:

    harden_signoff:
      floating_nets_waived:
        some_net: why floating is correct here

If the message says no log names them all, the run dir was trimmed: re-harden.
A count nobody can name cannot be excused.

The waiver clears this metric row. Harden also reads the final ODB, and there a
net with no driver stays an error: see `net_undriven`.
