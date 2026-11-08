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

## Results (2026-11-08): 27 of 28 pass

These pass and layoutlib draws them:
- `cap_nmos_03v3`, `cap_nmos_03v3_b`, `cap_nmos_06v0`, `cap_nmos_06v0_b`;
- `cap_pmos_03v3`, `cap_pmos_06v0`, and `cap_pmos_03v3_b`,
  `cap_pmos_06v0_b` (since #288);
- `diode_nd2ps_03v3`, `diode_nd2ps_06v0`, `diode_pd2nw_03v3`,
  `diode_pd2nw_06v0`;
- `diode_nw2ps_03v3`, `diode_nw2ps_06v0` (#291), `diode_pw2dw` (#287),
  `diode_dw2ps` (#286), `sc_diode` (#289);
- the six `npn_*`;
- the four `pnp_*` (since #285).

One fails, by design:

| model | fails | why |
|---|---|---|
| `cap_mim_2f0_m4m5_noshield` | tt_legal | the TT rule itself (#290): the precheck's `tech_data` forbids Metal5 (81/0) and its label on gf180mcuD, because the tile's own power grid is on Metal5, and this MIM's top plate is Metal5. Its row passes every other flag. The catalogue's `tt_note` puts this in the row's `tt_reason`, so the table records it as the rule, not as an open fault. A TT tile can't use it. |

### What the flow fixes

Each fix below is in the flow, not in a list. `layoutlib.magic_tech` writes a
patched copy of the PDK's magic tech for each extraction. It refuses a tech
that has neither the fault nor its fix, rather than guess. `devicelib.build`
draws what a pcell leaves out.

- **pnp (#285).** The magic tech picks the pnp name with
  `device msubcircuit pnp_* pnp pwell,space/w *pdiff error a2>..`. That
  makes the emitter (`*pdiff`) the substrate terminal, so the device has one
  terminal and `a2` (its area) is always 0. No selector applied, and the
  first rule (`pnp_10p00x00p42`) always won. `magic_tech` rewrites each rule
  to `device msubcircuit pnp_* pnp *pdiff pwell,space/w a1>.. a1<..`. The
  emitter is terminal 1, selected on its own area (the npn rules work the
  same way). The collector (`pwell,space/w`) is terminal 2, and there is no
  substrate terminal, since the collector is the substrate. ext2spice then
  writes `C B E`, the model's pin order, and each size extracts as itself.
  The areas are the PDK's. `corpus/ade/bandgap` uses `pnp_10p00x10p00`.
- **The well diodes (#286, #287, #291).** The tech has no device rule for
  `diode_dw2ps`, `diode_pw2dw` or `diode_nw2ps_*`, and it never reads their
  `WELL_DIODE` marker (153/51). `magic_tech` reads the marker into one well
  type per diode: `dnwdiode`, `pwdiode`, `nwdiode` and `mvnwdiode`. Outside
  the marker, those types connect, draw and write out as the plain wells.
  It adds one rule per diode, for example
  `device ndiode diode_dw2ps dnwdiode space/w,pwell`.
- **The DNWELL guard ring (#286, #287, #289).** DN.3 wants every DNWELL
  directly inside a PCOMP ring, and DF.18 wants any PCOMP outside the well
  to be 2.5 um from it. The PDK pcells' own ring (`pcmpgr`) comes out of
  gdsfactory as a filled square with no metal1. So `devicelib.build` draws
  `layoutlib.psub_ring` around the DNWELL of `diode_dw2ps` (where the ring
  is the anode), `diode_pw2dw` and `sc_diode`.
- **The diode_pw2dw anode pad (#287).** The pcell draws contacts in the
  LVPWELL anode but no metal1 over them. `build` draws that metal1
  (`m1_over`).
- **sc_diode (#289).** Two faults. `nsd` took the anode's edge, so the tech
  extracted three sc_diodes. It also counted the anode-cathode gap as
  anode: 5.12p of area for 4p drawn. The fix keeps `nsd` off SCHOTTKY and
  gives the closed gap to the cathode, so the cell extracts as
  `D0 A K sc_diode pj=8u area=4p`. The model has `cjp=0`, so it is an area
  device: the pcell and the reference netlist give w x l, and the area is
  what LVS checks. The PDK's klayout LVS counts the device differently. This
  flow uses magic and netgen, so it keeps the drawn area.
- **The P-varactors (#288).** `cap_pmos_03v3_b` and `cap_pmos_06v0_b` are a
  P+ poly gate on P+ diffusion in the substrate, with no well. The tech
  reads the gate as `pvaractor` (`mvpvaractor`) and names the device on its
  `*ppdiff` terminals. But it also counts the gate as a pfet area. It grows
  `pfetexpand` (`mvpfetexpand`) over the gate, so the source and drain read
  as pfet diffusion (`pdiff`/`pdc`), not `psd`/`psc`. For 3.3 V it also
  forces an nwell under the gate. The device rule found no terminal, so
  nothing extracted. The fix keeps a MOSCAP-marked P+ gate outside any
  nwell out of both rules. The cell then extracts as
  `X0 G SD cap_pmos_03v3_b c_width=2u c_length=2u`. A pfet, a pcap in its
  nwell, and a pfet drawn with no nwell all read as before. The full round
  trip changed no other row.

## Tests

`tests/test_devicelib.py` holds these.

Fast, on the shipped table: it passes each primitive the flow fixed, and
it records the Metal5 MIM's failure as the TT rule.

Fast, on a planted table:
- the readers;
- the wrappers' refusals;
- netlist_lint's three findings, plain, with a layout, in a TT tile and in
  an msde side;
- the extracted-netlist reader.

Slow:
- `--check` gives the table back. It runs on passing models of three kinds,
  a pnp and a P-varactor among them, plus the Metal5 MIM, which fails only
  `tt_legal`.
- Each DNWELL diode sits inside a PCOMP ring with no COMP within 2.5 um
  outside its DNWELL. Every anode contact of `diode_pw2dw` has metal1.
- A netgen setup with `cap_nmos_03v3`'s class misspelt gives
  `lvs_ok: false`. netgen compares the device as an empty black box
  (`layoutlib.LvsBlackBox`), and the round trip records that as an LVS
  failure, not a tool error.
- `layoutlib.mos_cap` draws a passing device, and `layoutlib.diode` and
  `layoutlib.mos_cap` draw fixed ones.

`tests/test_layoutlib.py` holds the `magic_tech` tests. Each tech fix has a
test on a fake PDK: the patch goes in, and a tech in neither form is refused.

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
