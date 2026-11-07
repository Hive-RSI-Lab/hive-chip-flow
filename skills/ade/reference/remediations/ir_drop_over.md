# ir_drop_over (em_ir)

A supply rail drops more than the bound from its pin to some extracted node
of it at the bench's operating point. The bound is spec.yaml's `ir_drop_mv`
when set, else 2 % of supply.vdd; the report's `ir_bound_source` says which.
The finding names the rail, the corner and the worst node (`<net>.nN`; its
position is in `log/em_ir/<top>.res.ext`).

**Cheapest fix first:** widen the rail where the current flows (a long
minimum-width strap is the usual cause), feed it from its pin at more than
one point, or add a parallel strap on another metal.

**Trap:** raising `ir_drop_mv` is a spec change, not a layout fix: only do
it when the design really tolerates the drop, and say why in spec.md.
