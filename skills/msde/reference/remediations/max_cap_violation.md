# max_cap_violation (top_harden)

The top's signoff STA counted pins driving more capacitance than their cell's
or the macro's `max_capacitance` allows at the named corner. The message
lists the pins from that corner's `checks.rpt`. Routes to `top_harden`.

**Cheapest fix first:** upsize or buffer the named driver, or split its
fanout. On a digital top port the load is `OUTPUT_CAP_LOAD` from the SDC;
pins on a ua[k] pad net are not counted. If the load really is fine, waive
the pin by name with a reason:

    harden_signoff:
      drv_pins_waived:
        some_pin: why this load is correct here

**Trap:** a waiver only excuses pins the report names. If the report is gone,
or names fewer pins than the count, re-harden. A count nobody can name cannot
be excused.
