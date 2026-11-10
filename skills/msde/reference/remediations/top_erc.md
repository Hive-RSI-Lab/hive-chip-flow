# top_erc

CVC (cvc_rv) ran on the flat transistor netlist of the assembled top and found
a device whose voltages break a reliability rule. Each finding carries `mode`,
the run that saw it, and `refs`, the device's path (`u_analog/xmp1`) and the
nets involved. The runs are under `log/top_erc/<mode>`: `cvc.error.gz` is the
full report, `<top>.cdl` the netlist CVC read, `cvc.power` the voltages.

- `operating`: the tile powered, every input and analog pad 0 V to the supply.
- `held`: `rst_n`, `clk` and every d2a crossing pin at 0 V.
- `analog_<cell>`: the macro alone, its power pins at the supply and ground.

By kind, cheapest fix first:

- **floating_gate**: see [floating_gate.md](floating_gate.md).
- **overvoltage_vgs / vbg / vds / vbs**: a terminal pair can see more than the
  device class is rated for (3.3 V devices 3.63 V, 5 V devices 5.5 V, 6 V
  devices 6.6 V). A 3.3 V device on a higher rail is an analog fix: a device
  rated for the net, or a level shifter on a crossing that needs one.
- **bulk_tie**, **forward_bias_diode**: a well or junction on the wrong net,
  usually a well tap inside the macro. An analog layout fix, as in the macro's
  own `erc` gate.
- **leak**: a path between two supply rails through devices CVC proved on. It
  can be a stack with both gates at their on rail, or two drivers fighting on a
  crossing net. Find the path from the named device.
- **possible_leak** (warning): CVC could not resolve a gate. Read it; if the
  gate can really reach the on state in some mode, treat it as a leak.

A finding inside the macro goes through `analog/`'s own router, then
`top_harden` again; one on the digital side through `digital/`'s. Nothing in
`top/` is edited by hand.

**Trap:** a waiver in `spec/erc_waivers.yaml` claims a device is safe. It names
the device by full path and needs a reason and an owner. Never waive a
floating gate or an overvoltage to get a pass.
