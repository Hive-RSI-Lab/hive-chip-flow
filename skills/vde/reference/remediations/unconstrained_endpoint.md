# unconstrained_endpoint (timing)

OpenSTA's `check_setup -unconstrained_endpoints` found endpoints (output
ports, register D pins) with no timing check: no output delay, or the
launching register is unclocked. Routes to `harden`.

**Cheapest fix first:** if `unclocked_register` is also reported, fix that
first - its D pins disappear from this list. An output port needs a
`set_output_delay` against its clock. A port that is meant to be
unconstrained (TT's `ena`, a static configuration pin) goes in spec.yaml
`timing.unconstrained_ok: [name, ...]` (globs allowed), with a reason in
the spec.
