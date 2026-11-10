# floating_gate (top_erc)

A gate in the flat top is on a net nothing drives. In `operating` or `held`
it is usually a macro input left unconnected: `refs` names the device and its
gate net, and a net named `<instance>.<pin>` is that macro pin, open in the
powered netlist.

**Cheapest fix first:** check the pin is an interface signal bound in the top's
`macros:` entry and reaches the macro in the digital side's wrapper. A missing
binding is a fix in the side that declares it, then `top_harden` again. In the
`analog_<cell>` run the floating gate is inside the macro: a device gate with
no driver in the sized netlist, an analog fix through `analog/`'s own router.

**Trap:** tying the pin off in `top/` hides it; `top/` is generated. See
[top_erc.md](top_erc.md) for the other kinds this gate reports.
