# antenna_violation (harden)

Antenna violations remain after LibreLane's repair. LibreLane only reports
these, so the design would otherwise ship with them. The finding names the nets
and the `report` it read them from. Routes to `harden`.

**Cheapest fix first:** check `RUN_ANTENNA_REPAIR` was not turned off in
`harden/config.override.json`. Then look at the nets: a long route to a single
small gate usually comes from pin placement, so buffering it near the far end
or moving the pin shortens it. If a net is in a macro (msde), fix it in the
macro's generator: the digital flow cannot repair inside a macro.

**Trap:** do not raise the limit in gates.yaml. A damaged gate shifts Vt in
silicon and no simulation shows it.
