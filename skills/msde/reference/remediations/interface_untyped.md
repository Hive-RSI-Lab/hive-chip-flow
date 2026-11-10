# interface_untyped (split, boundary)

`interface.yaml` is not the typed form the boundary gate measures against.

**Cheapest fix first:** set `version: 2`, name each `level` from
`engine/reference/levels.yaml`, and write each `load` as `{cap_ff: N}` or
`{pin: <liberty cell>/<pin>}`, in `interface.yaml` and both side specs.

**Trap:** an estimated capacitance goes in `cap_ff` with a `note` saying so.
Do not leave the old free-text string; nothing can measure it.
