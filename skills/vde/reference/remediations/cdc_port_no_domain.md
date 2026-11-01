# cdc_port_no_domain (cdc)

A flop reads an input port, the design has several domains and spec.yaml has
no `clock.primary`, so the port has no known domain. Routes to `rtl`.

**Fix:** set `clock.primary`, or declare the port under `ports` in
`spec/cdc.yaml` (`async` or a domain name).
