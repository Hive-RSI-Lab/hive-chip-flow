# survivor_cap_halved (bench_strength)

A capacitor listed in spec.yaml's `devices` (a PDK `cap_*` instance's
`c_width`, or a plain `c...` element's value) was halved, and no measure
moved out of bound or past its declared `sensitivity`. Routes to
`testbench`.

**Cheapest fix first:** a compensation or filter cap sets a pole, so its
effect shows in a frequency-domain measure: phase margin, unity-gain
frequency, a corner frequency or a settling time. If the block is in a
loop, the bench needs a stability measure (`skills/ade/templates/stb_tb.cir`:
`pm_deg`, `gm_db`, `ugf_hz`) bounded at the spec's `min`; add a
`sensitivity` on `ugf_hz` or `pm_deg` when a halved cap still meets the
spec. A DC-only bench cannot see a cap at all.

**Trap:** removing the cap from `devices` makes the gate warn
`device_undeclared` instead, which hides the weakness rather than fixing
it.
