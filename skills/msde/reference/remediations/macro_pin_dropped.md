# macro_pin_dropped (top_harden)

A macro pin that `interface.yaml` names as a crossing signal or a `ua_pins` entry
isn't connected on the macro's instance in the final netlist. Routing never saw it,
so it reaches no pad or cell. `top_lvs` won't catch this: it compares the layout
with a netlist that lost the same pin. `precheck` sees it only for an analog pad.

**Cheapest fix first:** look for `STA-0201 ... port <pin> not found` in the run's
`flow.log`. That means the macro's liberty doesn't name the port. `macro_lib`
writes every port of the cell, so re-run it if the libs predate that, then re-run
`top_harden`.

**Trap:** removing the pin from `ua_pins` or `analog_pins` clears the finding and
leaves the pad unconnected on the chip.
