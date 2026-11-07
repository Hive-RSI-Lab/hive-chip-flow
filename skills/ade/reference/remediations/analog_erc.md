# analog_erc (erc)

CVC (cvc_rv) ran on magic's extraction of the regenerated layout and found a
device whose voltages break a reliability rule. Each finding names the
extracted device (`device`, e.g. `X3`), its schematic `refdes` when one
netlist device matches it, its `model`, every terminal's net with the
min..max voltage CVC propagated to it (`terminals`), and its `position` in
um in the layout. The full report is `log/erc/cvc.error.gz`; the netlist CVC
read is `log/erc/<top>.cdl`, the pin voltages `log/erc/cvc.power`. With
`--schematic` (a block with no layout yet) the same files are under
`log/erc_schematic/` and a finding names the netlist/<block>.cir device.

By kind, cheapest fix first:

- **overvoltage_vgs / overvoltage_vbg / overvoltage_vds / overvoltage_vbs**:
  a terminal pair can see more than the device class is rated for (3.3 V
  devices 3.63 V, 5 V devices 5.5 V, 6 V devices 6.6 V). Check where the
  high net comes from: a pin `erc_nets` gives a range above the device's
  rating, or a 3.3 V device tied to a 5 V or 6 V net. Use a device rated
  for the net (`nfet_06v0`, `pfet_06v0`) in the netlist and the generator,
  or move the device off the net. If the pin range is wrong, fix
  `erc_nets` in spec.yaml, which is a spec change.
- **bulk_tie**: an NMOS bulk that can sit above its source or drain, or a
  PMOS bulk that can sit below them. The usual cause is a well tap on the
  wrong net: a PMOS whose nwell tap is wired to vss, or a psub tap on a
  signal. Wire the tap to the right rail in the generator. If the netlist
  says the same thing, the design is wrong and the netlist changes too.
- **floating_gate**: a gate on a net nothing drives. Look for a missing
  via or wire to the gate, or a gate the netlist also leaves open.
- **forward_bias_diode**: a diode (often a well or junction diode) whose
  anode can sit above its cathode. Check its two nets.
- **overvoltage_junction** (a BJT, `junction` names it, e.g. `eb`): one of
  a bipolar transistor's junctions can see more than the 3.3 V LV rating
  (3.63 V). GF180's BJTs are all LV devices: keep the emitter, base and
  collector inside 0..3.63 V of each other, or move the BJT to a
  lower-voltage net. A wrong pin range is an `erc_nets` fix.
- **substrate_not_ground / substrate_tie** (a BJT): a vertical PNP's
  collector, or an NPN's substrate terminal, is the p-substrate and must
  be the ground net. In layout, a collector off vss means no psub tap
  reaches it, or the netlist draws a collector the silicon cannot have.
  Tie the substrate to vss, and keep the base from going below it
  (substrate_tie: the substrate junction can forward bias).
- **voltage_conflict**: a pin range that the circuit contradicts, e.g. a
  current input on a diode-connected device given a 0..vdd range. Set that
  pin's real range in `erc_nets` (spec.yaml).
- **model_error, unexpected_voltage, fuse, erc_unparsed**: read the CVC
  report; erc_unparsed means CVC counted more than the gate could read.
  That is a gate bug to report, not a design fix.
- **erc_waiver_unused**: a waiver in `spec/erc_waivers.yaml` that matches
  no finding. Remove it.
- **leak / possible_leak / gate_vs_source / ldd** (warnings): CVC's logic
  checks. A path from a driven input to a rail through a low resistance is
  either the design's intent or a short. Read the nets it names.

**Waivers** (`spec/erc_waivers.yaml`) are for a finding that is safe in
this design: one entry per device and kind, with every net as reported, a
`reason` and an `owner`. Prefer fixing the design: a waiver needs the
person's sign-off, like any spec change (`erc_waiver_edit`, hold 2).

**Trap:** never widen a device's rating, drop a pin from the power file or
narrow a pin range below what the block really sees just to pass. The
range in `erc_nets` must be the real one.
