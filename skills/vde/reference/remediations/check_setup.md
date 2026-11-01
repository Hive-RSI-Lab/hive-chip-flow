# check_setup (timing)

OpenSTA's `check_setup` printed a warning heading the gate does not
know by name (a newer OpenSTA). The finding's message carries the
heading and items. Routes to `harden`: read it and treat it like the
named check_setup kinds (unclocked_register, unconstrained_endpoint,
generated_clock_unreachable).
