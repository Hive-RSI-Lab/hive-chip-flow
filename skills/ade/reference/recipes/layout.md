# layout - generator code to a GDS that passes drc, lvs and pex_sim

Layout is code (docs/design.md 5). The layout-writer writes
`layout/gen_<block>.py`, `layout_gen.py` runs it, and klayout, netgen and
magic only judge the result. Nobody edits a GDS, ever: every gate rebuilds
it from the generator on each run, so a hand edit is overwritten and proves
nothing.

## Before the layout-writer starts

`sim_pvt` should be fresh-pass. LVS compares the layout against
`netlist/<block>.cir` at `sizing/sizing.yaml`'s values, so drawing a layout
for a netlist that is still moving wastes a layout pass. The pex bench
(`layout_ref/<block>_pex_tb.cir` and its `.bounds.json`) must exist too.
If it doesn't, spawn the bench-writer in PEX MODE first.

## What the generator must match

- Device sizes: the netlist's W/L and resistor lengths, after sizing.yaml's
  overrides. A generator constant that drifts from the netlist is the
  fault `lvs` is planted with.
- Pin labels: the netlist's subckt pin names, written in subckt order
  (magic numbers extracted ports in label order, and the pex bench binds
  them by position).
- `layoutlib.finalize()` last, which snaps to the grid and flattens.

## The three gates, and who fixes what

| gate | reads | a failure goes to |
|---|---|---|
| drc | klayout GF180 signoff deck, per rule | layout-fixer (`analog_drc.md` remediation, by rule family) |
| lvs | netgen, layout vs netlist at its sizing | layout-fixer, unless the netlist is what is wrong |
| pex_sim | magic extraction with R and C, pex bench at each `pex_corners` corner and `pex_rc_knobs` knob | layout-fixer for `measure_out_of_bounds`, bench-writer for `measure_missing` |

Declare `layout_code_edit` after every generator change, including each
fix, and re-run the gate that failed. A pex bench change also needs a
declared `layout_code_edit`: `layout_ref/` is not one of `pex_sim`'s
hashed inputs, so only the mark stales it.

## Honest limits

`pex_sim` runs its corners on magic's extractor only. klayout_pex is not
wired in, and wire R and C do not move with the corner (a corner moves the
devices, the passives, the temperature and the supply). `pex_rc_knobs: [c_high]`
or `all` in spec.yaml adds a run per knob with every R and C scaled by a pinned
factor, at the slow/hot corner for a `_high` knob and fast/cold for a `_low`
one. The factors (engine/reference/pex_rc_knobs.yaml) are an approximation, not
foundry RC corners, and each knob is one more ngspice run, so turn them on for
a block whose speed a wire limits. Absent a `pex_corners`, the corners are typical, slow/hot and
sim_pvt's worst per measure; a run that must see every corner says
`pex_corners: all`. An
`rm1` poly resistor extracts through a per-run patched copy of the PDK's
magic tech file (docs/spikes/glayout.md). If the PDK moves that line,
extraction refuses rather than calling the resistor a short.
