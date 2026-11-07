# match_no_units (layout_match)

A device named in spec.yaml's `matching` has no unit in the extracted
layout: no extracted transistor's model, gate, source and drain fit that
netlist device. Routes to `layout` (the layout-fixer) - but read it first:
usually the layout wires the device to the wrong nets (which `lvs` also
reports), or the device's name in `matching` is spelt differently from
`netlist/<block>.cir`.

**Fix:** if `lvs` fails, fix that first; this finding goes with it. If
`lvs` passes, the name in `matching` is wrong: that is the spec's
author's fix (the name is the netlist's instance name, e.g. `xmref`).

**Trap:** never remove the device from `matching` to clear this.
