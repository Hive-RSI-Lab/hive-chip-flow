# load_untyped (split)

A signal's `load` is not `{cap_ff: N}` or `{pin: <liberty cell>/<pin>}`
(optional `count`, `note`). The message says which part is wrong.

**Cheapest fix first:** name the digital cell input the signal drives, by
its full liberty name, or give the capacitance in femtofarads.
