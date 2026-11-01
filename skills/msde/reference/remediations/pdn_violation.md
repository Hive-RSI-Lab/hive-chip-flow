# pdn_violation (harden)

`design__power_grid_violation__count` is above zero: instances or macros are not
connected to the power grid, so the part does not power up where they sit.
Routes to `harden`.

**Cheapest fix first:** read the PDN step's log for the instance names. For a
macro (msde) check `macro_power_unreachable`'s fix: a macro's power pins must
land under a Metal4 strap. Do not edit the generated `macro_pdn.tcl` by hand.
