# bad_erc_nets (spec_lint)

`erc_nets` in spec.yaml is not a mapping of pin name to `{power: volts}` or
`{min: volts, max: volts}` with min <= max. The `erc` gate refuses it too
(exit 2), and also refuses a pin name the extracted cell does not have.

**Cheapest fix first:** name only the pins whose range is not the default,
e.g. `erc_nets: {iref_node: {min: 0, max: 0.6}}`. Without an entry, `vdd`
is a supply at supply.vdd, `vss`/`gnd` are 0 V, and every other pin is an
input from 0 to supply.vdd.

**Trap:** the range is what the pin really sees in the system, not what
makes CVC quiet. A narrower range than the real one hides an overvoltage.
