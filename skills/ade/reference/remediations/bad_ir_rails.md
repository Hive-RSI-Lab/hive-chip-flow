# bad_ir_rails (spec_lint)

`ir_rails` in spec.yaml is not a list of distinct pin names. It names the
pins the `em_ir` gate checks IR drop on. Leave it out to take the pins
named like a `supply` key or vss/gnd. Set `ir_rails: []` only for a block
with no supply pin at all (a passive ladder): the gate then checks EM
alone and reports `ir_checked: false`. A name the block's pins do not have
is a refusal at the gate.
