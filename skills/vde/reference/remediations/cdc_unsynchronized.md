# cdc_unsynchronized (cdc)

A flop samples a signal from another clock domain and its output goes on to
logic, so a metastable value can reach more than one flop. Routes to `rtl`.

**Cheapest fix first:** add a 2-flop synchroniser in the destination domain:
the first flop reads the source with no logic between, and its Q feeds only
the second flop, on the same clock and edge.

If the signal really is quasi-static, or the single-flop sample is the design,
that is a decision for the person: a `{from, to, reason}` waiver in
`spec/cdc.yaml`. If an input port is simply in another domain than
`clock.primary`, declare it under `ports` there (`async` or the domain).
