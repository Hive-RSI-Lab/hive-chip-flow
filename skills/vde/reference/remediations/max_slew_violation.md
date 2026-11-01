# max_slew_violation (harden)

STA found a pin over its max slew limit at the corner the finding names, in
the last post-route STA. LibreLane only reports this. The finding lists each
pin with its value and limit, and the `report` it read them from. Routes to
`harden`.

**Cheapest fix first:** read the pins. A cell output with a port on the same
net, driving the flow's output load, wants a stronger driver: check why repair
left it at this corner. A macro pin means the digital side overdrives or
underdrives what `macro_timing` declares.

If the pin carries no digital edge at all (an analog pad, a DC bias), waive that
pin with a reason. Each pin is waived on its own, at every corner:

    harden_signoff:
      max_slew_waived:
        "ua[2]": why no digital edge reaches this pin

A real digital pin is a fix, not a waiver. A pin whose limit `macro_timing`
declares can't be waived (`macro_limit_waived`).

**Trap:** do not raise the limit in gates.yaml or the flow's max transition. If
the message says the report names too few pins, the run dir was trimmed:
re-harden.
