# measure_bad_pad

A spec measure's `pad` key is not one of `both`, `bare` or `pad`, or a
measure sets `pad` in a spec that lists no `ua_pins`.

`pad` says which ua pad condition scores the measure: `both` (the default),
`bare` (the `no_pad` run only: the measure assumes a bare pin, and the spec
says so), or `pad` (the pad run only). Fix the value, or drop the key when
the block has no ua pin.

**Trap:** `pad: bare` is a statement about the contract, not a way to pass.
Use it only for a measure that really is about the bare die port (an
internal node, or a figure the datasheet quotes before the pad), and say so
in the requirement text.
