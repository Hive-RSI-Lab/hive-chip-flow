# PDK device coverage: devices.yaml (#137)

`engine/scripts/pdk_devices.py` round-trips every gf180mcuD primitive the
flow has no gate for yet: MOS capacitors, diodes, BJTs, and the MIM cap.
It writes one row per model to `engine/reference/devices.yaml`.

```
bin/eda python engine/scripts/pdk_devices.py --out $TMPDIR/pdk --jobs 4
bin/eda python engine/scripts/pdk_devices.py --out $TMPDIR/pdk --check
```

Each device, at each size `engine/lib/devicelib.py`'s `CATALOG` gives it,
goes through these steps. Each step is one flag:

| flag | step |
|---|---|
| `drc_ok` | draw the PDK pcell, with a tap and labels (`devicelib.build`); klayout DRC is clean |
| `extract_ok` | magic extracts exactly one device of that model name |
| `lvs_ok` | netgen matches the extraction against a one-device schematic |
| `sim_ok` | ngspice loads the model at each of its kind's sections (`moscap_*`, `diode_*`, `bjt_*`, `mimcap_*`) and gives a finite, non-zero value |
| `tt_legal` | the GDS passes the TT precheck's two layer checks (`ttlib.gds_layer_problems`, the vendored `tech_data`) |

A row's flag is true only when every size passed. `reason` names the first
step that failed; `tt_reason` names the layers a TT tile may not have.
`--check` reruns the round trip and writes nothing: exit 1 if a row's flags
differ from the table's.

## Who reads it

- **layoutlib** draws a device only once its row passes every flag:
  `layoutlib.mos_cap(model, lc, wc)`, `layoutlib.diode(model, la, wa)` and
  `layoutlib.bjt(model)`. A model with no row, a failed row, or the wrong
  kind raises `LayoutError` (`devicelib.not_ready`).
- **netlist_lint** checks each device the netlist uses (an `x` subckt line,
  or a `d` diode line) against its row. It does not judge a model with no
  row: the FETs and resistors have gates of their own.
  - `device_not_lvs_ok`: an error when the workspace has a layout
    generator (`layout/gen_*.py`), and a warning before then: an empty
    `layout/` scaffold does not count. Gates fail only on errors (`gates.yaml`'s
    `fail_severities`), so an analog block can still pick a device before
    it has a layout.
  - `device_not_tt_legal`: an error in a TT tile. A block is a TT tile when
    spec.yaml says `tt_tile: true`. With no `tt_tile` key, it is one when
    its workspace is an msde side (`interface.yaml` in the parent).
    `tt_tile` must be true or false (`bad_tt_tile`).

The TT check is in netlist_lint, not spec_lint as #137 suggested.
`engine/reference/invalidation.yaml` gives ade's spec_lint only `spec.yaml`
as input, so a check there would not rerun when the netlist changed.
netlist_lint reads the netlist and spec.yaml, and its record goes stale
with either.

## Results (2026-11-08): 20 of 28 pass

These pass and layoutlib draws them:
- `cap_nmos_03v3`, `cap_nmos_03v3_b`, `cap_nmos_06v0`, `cap_nmos_06v0_b`;
- `cap_pmos_03v3`, `cap_pmos_06v0`;
- `diode_nd2ps_03v3`, `diode_nd2ps_06v0`, `diode_pd2nw_03v3`,
  `diode_pd2nw_06v0`;
- the six `npn_*`;
- the four `pnp_*` (since #285, below).

These fail:

| model | fails | why |
|---|---|---|
| `cap_pmos_03v3_b`, `cap_pmos_06v0_b` | extract, LVS | magic extracts no device of that name from the pcell: the pcell and the magic tech disagree |
| `diode_nw2ps_03v3`, `diode_nw2ps_06v0` | extract, LVS | the magic tech has no device of that name (it has `nw2pw`, `dnw2pw`, `dnw2ps`) |
| `diode_pw2dw` | everything but sim | the pcell draws no metal1 pad for the anode, so nothing is drawn |
| `diode_dw2ps` | DRC, extract, LVS | DN.3 (a DNWELL needs a PCOMP guard ring), DF.18 |
| `sc_diode` | DRC, extract, LVS | DN.3 |
| `cap_mim_2f0_m4m5_noshield` | tt_legal | Metal5 (81/0) and its label are forbidden in a TT tile: the tile's own power grid is on Metal5 |

The pnp rows were a PDK tech finding (#285). The magic tech picks the pnp
name with `device msubcircuit pnp_* pnp pwell,space/w *pdiff error a2>..`:
that makes the emitter (`*pdiff`) the substrate terminal, so the device has
one terminal and `a2` (its area) is always 0. No selector applied, and the
first rule (`pnp_10p00x00p42`) always won. `layoutlib.magic_tech` rewrites
each rule to `device msubcircuit pnp_* pnp *pdiff pwell,space/w a1>.. a1<..`:
the emitter is terminal 1, selected on its own area (the npn rules work
the same way), the collector (`pwell,space/w`) is terminal 2, and there is
no substrate terminal (the collector is the substrate). ext2spice then
writes `C B E`, the model's pin order, and each size extracts as itself.
The areas are the PDK's. `magic_tech` refuses a tech with no pnp rule or a
pnp rule in neither form. `corpus/ade/bandgap` uses `pnp_10p00x10p00`.

## Tests

`tests/test_devicelib.py` holds these.

Fast, on a planted table:
- the readers;
- the wrappers' refusals;
- netlist_lint's three findings, plain, with a layout, in a TT tile and in
  an msde side;
- the extracted-netlist reader.

Slow:
- `--check` on passing models of three kinds, a pnp among them, plus a
  `cap_pmos_*_b` that fails at extraction, gives the table back.
- A netgen setup with `cap_nmos_03v3`'s class misspelt gives
  `lvs_ok: false`. netgen compares the device as an empty black box
  (`layoutlib.LvsBlackBox`), and the round trip records that as an LVS
  failure, not a tool error.
- `layoutlib.mos_cap` draws a passing device.

## Not covered

- `simlib` has corner tokens for the FET, resistor and MIM sections
  (`{{CORNER}}`, `{{RES_CORNER}}`, `{{MIM_CORNER}}`), but none for
  `moscap_*`, `diode_*` or `bjt_*`. A bench that uses one of these devices
  must name the section itself, so sim_pvt does not vary it across corners.
  `sim_ok` loads each section directly.
- Two sizes per device (2x2 and 4x2 um; the MIM at 10x10 and 20x10 um),
  and the BJTs at their fixed layout. LVS property checks across a wider
  size range are not exercised.
- The FETs and resistors are not in the table: they have their own gates.

## PLL debt (for pll-paper-update)

`blocks/pll/analog/netlist/pll_analog.cir:112-113` (`XC1`, `XC2`) uses
`cap_mim_2f0_m4m5_noshield` in a TT tile (an msde side), and
`gen_pll_analog.py` draws it with `metal_level="M5"`. The GDS has Metal5
(81/0), which the TT precheck forbids. netlist_lint now reports
`device_not_tt_legal` on both. blocks/pll was left alone.
