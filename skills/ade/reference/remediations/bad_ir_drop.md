# bad_ir_drop (spec_lint)

`ir_drop_mv` in spec.yaml is not a positive number. It is the `em_ir`
gate's bound, in mV, on each supply rail's static drop from its pin to any
node of it. Leave it out to take 2 % of supply.vdd, or set the drop the
design tolerates, e.g. `ir_drop_mv: 3` for a mirror whose source must sit
within a few mV of vss.
