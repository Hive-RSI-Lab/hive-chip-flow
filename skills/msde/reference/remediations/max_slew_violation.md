# max_slew_violation (top_harden)

The top's signoff STA counted pins whose transition is over the limit
(`MAX_TRANSITION_CONSTRAINT`, or a macro's `max_transition`) at the named
corner. The message lists the pins from that corner's `checks.rpt`. It is
usually an ss corner. Routes to `top_harden`.

**Cheapest fix first:** look at what drives each named pin. A digital output
behind a `buf_1` or `inv_1` that drives a pad load wants a bigger cell or a
buffer. Pins on a ua[k] pad net are not counted (the report's `drv_metrics`
lists them as `analog_pad`): STA sees only LibreLane's default drive and load
there. If the slew really is harmless, waive the pin by name with a reason:

    harden_signoff:
      drv_pins_waived:
        some_pin: why this slew is correct here

**Trap:** a waiver only excuses pins the report names. If the report is gone,
or names fewer pins than the count, re-harden. A count nobody can name cannot
be excused.
