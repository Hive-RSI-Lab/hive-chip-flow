# cosim - both sides in one bench

`cosim` builds `{ws}/tb/`'s Verilog with Icarus and runs its cocotb tests,
with the analog block simulated by ngspice through cocotbext-ams's
`MixedSignalBridge`. So one bench drives both sides: the digital side
reacts to transitions ngspice actually computed.

It is not built on ngspice's `d_cosim`/`ivlng` bridge, which is broken in
this image. `docs/spikes/dcosim.md` says why and records the choice
`docs/design.md` section 5 asked for.

## What the bench must provide

- `tb/cosim_bench.json`: `top`, `bounds`, `analog_netlist`, `analog_kind`
  (`ideal` or `transistor`).
- A bench that writes `reports/cosim_measures.json` BEFORE any assertion
  can raise: every bounded measure, `digital_toggles` backed by that many
  `edge_times_ns`, and `spice_time_reached_ns`.
- For a transistor-level top, a bench that runs at a corner:
  `tb/`'s `analog_netlist` is a thin wrapper that instantiates the cell,
  the bench applies `cosimlib.corner_setup()` when it is not None (its
  `lines` go into the deck, the cell's `.include` among them), and it adds
  `cosimlib.corner_evidence(setup)` to its measures.

## The corner runs

The typical runs (the schematic and the RTL, then the ua pads when the
block has them) come first and stay as they are. When they pass, the gate
runs the same bench again at each corner, under
`log/cosim_corners/<view>/<corner>/`, each with its own copy of `tb/` and
the block's `analog/`, `digital/` and `interface.yaml` linked beside it,
so a bench that finds `../analog` from its own file still works. `tb/cosim_bench.json`'s optional
`corners` key sets:

- `bound_by`, a list of `[measure, "min"|"max"]`, or `list`, corner names.
  The default is the corners at the end of every measure in the analog
  side's recorded extracted sweep (`gate-pex_sim.json` or `pex_pvt.json`).
- `digital`, a list of `rtl` and `gl`. The default is `gl` when the digital
  side's harden has a final netlist, else `rtl`. `gl` runs that netlist with
  the SDF of the corner's process and nearest temperature, written with
  OpenSTA when the harden did not write that corner.
- `env`, `timeout_s` and `jobs`, for the bench's environment, the limit on
  one run and how many run at once.

The analog view is the extracted netlist the recorded sweep ran on, or the
schematic when the analog side has no sweep. A corner run is the bare cell:
the pad condition stays at typical. A released v1 block with no `corners`
key is not held to this, and the facts say so.

## What the gate does not trust

cocotb's own pass alone, and ngspice's silence. It scans the sim log for
non-convergence and for an ignored `.ic`, requires evidence the digital
side toggled and the analog side ran far enough, then checks every bound.
A failing cocotb test is reported as well, never instead.

## Routing a failure

The bench is the integrator's. A finding that traces into a side's design
(a divider that divides by the wrong ratio, a control word driven with the
wrong polarity) is fixed in that side's nested workspace through its own
router and gates, then `cosim` re-runs here. Never loosen a bound or edit
`reports/cosim_measures.json` to make it pass.

`cosim`'s recorded inputs are `interface.yaml`, `tb/`, `digital/rtl`,
`analog/netlist`, `analog/sizing/sizing.yaml`, the analog side's extracted
netlist and its recorded sweeps, and the digital side's final netlist, SDF,
SPEF and SDC, so a fix inside a side that touches any of those marks `cosim`
stale. The post-layout ones count only where the block owes corner runs. The
extracted netlists and final views stay out of git, so a pass leaves their
hashes in `reports/recorded/cosim_views.json`, which a clone reads in their
place: commit it with the rest of the block's records.
