# r2r_dac_analog - the 8-bit R-2R ladder macro of r2r_dac

The analog side of the `/msde` block `r2r_dac` (../brief/spec.md is the
brief this file is written from; ../../brief/spec.md the top-level one).
An 8-bit R-2R ladder in gf180mcuD poly resistors, switched by eight
gf180mcu_fd_sc_mcu7t5v0 standard-cell drivers at 3.3 V, whose output
leaves the Tiny Tapeout tile on analog pin `ua[0]`. A hard macro inside
the digital tile, built and checked as a standalone `/ade` block: netlist
as code, layout as a generator, ngspice for every number, netgen LVS clean.

## Behaviour

`vout = dac_code / 256 * VDD` (ideal), VDD = 3.3 V, so 1 LSB = 12.89 mV
and full scale is 255/256 x 3.3 V = 3.287 V.

- Eight switch drivers, one gf180mcu_fd_sc_mcu7t5v0 buffer cell per bit,
  one drive strength for all eight (`buf_16` or the largest available,
  whichever gives the lowest on-resistance - the analog-designer picks and
  states the measured pull-up and pull-down Ron at tt/25 C here). Each
  driver's output drives the 2R branch of its bit; its input is the
  crossing signal `dac_code[k]`. The drivers live in this macro, on the
  macro's own vdd/vss, instantiated from the PDK's cell spice library and
  placed from the PDK's cell GDS; they are the only active devices, so the
  ladder and the driver Ron are one accuracy budget this side's bench
  measures directly and netgen LVS covers the whole signal path.
- Ladder: standard R-2R, 8 rungs, terminated with 2R to vss at the LSB
  end. GF180 unsalicided p+ poly resistor `ppolyf_u` (sm141064.spice:
  rsh 350 ohm/sq typical, 420 ss, 280 ff; tc1 = -0.9e-4/C). Every R is one
  unit element and every 2R two units in series, same W and L everywhere,
  so the ratio is set by matching, not absolute value.
- Driver Ron absorption: the driver's Ron (tens of ohms) is in series with
  each 2R branch. The drawn 2R branch is 2R - Ron_nom, Ron_nom the average
  of pull-up and pull-down Ron at tt/25 C, trimmed by a fraction of a unit
  or by the unit's length, so tt/25 C INL meets +-0.5 LSB. The residual
  across PVT (Ron moves +-30 % while poly R moves much less, and pull-up
  and pull-down Ron differ) is what the corner report shows - the brief
  expects it to exceed +-0.5 LSB at ss/125 C and ff/-40 C; report it, do
  not hide it and do not widen the bound.

### Values chosen (the analog-designer revises with numbers at P2)

- Pad load ASSUMED: TT analog pin `ua[0]` modelled as C_pad = 5 pF (pad +
  ESD + package) plus 2 pF tile-to-pad routing, in series with R_pad =
  100 ohm (pad/ESD series resistance): R_pad = 100 ohm, C_load = 7 pF to
  vss. The TT analog pad's real load is not documented in this repo; the
  bench carries both as one parameter each so they can be changed.
- R = 250 ohm (2R = 500 ohm). Thevenin output resistance is R plus about
  Ron/2, so tau = (250 + ~30 + 100) x 7 pF = 2.7 ns; settling to 0.5 LSB
  of 8 bits is ln(512) = 6.24 tau = 17 ns, inside one 50 MHz period
  (20 ns) at tt; ~19 ns at ss/125 C with poly R +20 % and Ron +30 %.
  Cost: the drivers see ~3R = 750 ohm, up to 4.4 mA per bit, and the
  termination draws 6.6 mA at full scale; total supply current a few to
  ~15 mA. If measured settling misses 20 ns at the worst corner, lower R
  to 200 ohm before anything else; if the current budget is the problem,
  raise R and report settling as multiple periods - both go to the owner
  as OPEN, never silently.
- Unit resistor: W about 2 um (chosen for matching, not minimum) at the
  length that gives R = 250 ohm with rsh 350 ohm/sq (L/W = 0.714, i.e.
  L ~ 1.43 um plus the model's end corrections - P2 checks it against the
  PDK's minimum length; if 250 ohm needs a length below it, 2 units in
  parallel per R and 4 per 2R with 2 in series). P2 states the drawn W, L
  and the expected sigma(R)/R. What the PDK offers for that sigma: no
  per-instance mismatch data at all for ppolyf_u (next section); the only
  statistical numbers are global - rsh agauss(0, 18.116, 3) on 350 ohm/sq
  (~1.7 % 1-sigma, all units together) and dW agauss(0, 0.0167 um, 3).

### Resistor mismatch: what the PDK models support

Checked in the eda toolchain's PDK, `foss/pdks/gf180mcuD/libs.tech/
ngspice/sm141064.spice` and `design.spice`:

- `.subckt ppolyf_u` scales its body resistor by
  `(1 + mis_r * sw_stat_mismatch)`, but `mis_r=0` is a subckt-line default
  and nothing in the model file ever assigns it an `agauss()` - unlike
  `.lib fets_mm`, which draws `mis_vth`/`mis_k` per FET instance. Setting
  `sw_stat_mismatch=1` (what check_mc.py injects) therefore leaves every
  poly resistor exactly nominal.
- `.lib res_statistical` provides `mc_rsh_ppolyf_u`, `mc_dw_ppolyf_u`,
  `mc_rt_ppolyf_u` as `agauss()` draws gated by `sw_stat_global` - one
  draw per deck shared by every instance. That is global (lot) variation:
  it moves every unit together and cannot change a ratio, so it cannot
  move DNL or INL.
- `.lib typical/ss/ff/sf/fs` do not include `res_typical/res_ss/res_ff`
  or `.lib res` at all; the bench must include the resistor corner section
  itself. There is no `res_sf`/`res_fs`: the poly-resistor corner files
  do NOT give sf/fs corners (the brief's "add sf/fs if they exist" is
  answered no for the resistor; the engine's sf/fs default corners still
  run, with the FETs skewed and the resistor at whichever section the
  bench pairs them with).

So Monte-Carlo resistor mismatch is NOT supported by these models and
`mc.enabled` is false. The brief's fallback replaces it: a deterministic
sensitivity, each unit resistor perturbed +1 % and -1 % one at a time with
all others nominal, the 256-code INL sweep re-run each time, worst
max |INL| reported (`inl_sens_1pct_max`).

## Interface

`.subckt r2r_dac_analog dac_code[7] dac_code[6] dac_code[5] dac_code[4]
dac_code[3] dac_code[2] dac_code[1] dac_code[0] vout vdd vss`, in that pin
order. Supply 3.3 V, one vdd and one vss pin.

- `dac_code[7:0]` - the crossing signal ../../interface.yaml and
  ../../analog_spec.yaml name: direction d2a, level cmos_3v3, domain
  clk_sys, width 8. Eight CMOS inputs, one driver cell input each; the
  digital side drives them from a register clocked at 50 MHz. Bit 7 is
  the MSB. This side's bench drives them itself as ideal 3.3 V
  piecewise-linear sources with 100 ps edges.
- `vout` - the ladder output. NOT a crossing signal: it does not go to the
  digital side; it leaves the tile on TT analog pin `ua[0]` (the one
  analog pin the top brief allows), routed at `top_harden` from the macro
  pin to the tile's `ua[0]`. Nothing in the digital netlist touches it.
  If the nested flow or `top_harden` cannot express a macro pin that goes
  to a tile pad rather than to the digital side, stop and report that; do
  not fold `vout` into the digital side.
- `vdd` / `vss` - the single 3.3 V supply and ground, shared with the
  digital tile.

Not on this side: the code source (register, ramp, sine) is the digital
side; code-to-voltage measures across the whole tile are top-level cosim
measures.

## Corners

The brief asks for process tt/ff/ss x temperature -40/25/125 C, nine
corners, all at VDD = 3.3 V, tt/25 C reported as "typical" and the worst
corner per measure. The engine cannot express that set: `corners.py`
resolves a spec's `corners` list only against `engine/reference/
corners.yaml`'s five `default_corners` (tt 27 C/3.3 V, ss 125 C/2.97 V,
ff -40 C/3.63 V, sf 125 C/2.97 V, fs -40 C/3.63 V), there is no
fixed-supply point and no tt-at-125 C or ss-at-minus-40 C point, the
temperature axis has 27 C not 25 C, and a per-measure `corners` list is
lint-only (check_sim_pvt reads the top-level field and unions it with the
five). This spec therefore says `corners: default` and every bound is
written in LSB units, seconds or amperes - never in volts that scale with
VDD - so the +/-10 % supply corners test the design rather than the unit.
The nine-corner request is carried in the spec-writer's OPEN for the
engine owner (a corners.yaml change is a reviewed engine change, never a
per-block edit).

## Measures

Every measure at the ladder output into the assumed pad load, every
corner of the set above; the 256-code sweep holds each code long enough
to settle (e.g. 200 ns) and reads vout at the end of each step; LSB_meas
= (v(255) - v(0)) / 255 (end-point fit). "Source" says whether the bound
is a number the brief states (brief) or one derived here because the
brief asks only to report the quantity (derived - all in OPEN for the
owner). A derived bound is carried at severity warning; a brief bound at
error. Neither is ever widened to make a design pass.

| measure | what | unit | bound | source |
|---|---|---|---|---|
| dnl_max_abs | max over k of abs((v(k)-v(k-1))/LSB_meas - 1) | LSB | <= 0.5 | brief |
| inl_max_abs | max over k of abs((v(k) - endpoint fit(k))/LSB_meas) | LSB | <= 0.5 | brief |
| monotonic | 1 if v(k) > v(k-1) for all k = 1..255, else 0 | - | >= 1 | brief |
| v_zero_offset | v(0), the zero-code offset from vss | V | <= 6.45e-3 (0.5 LSB) | derived |
| fs_gain_err_lsb | ((v(255)-v(0)) - 255/256 VDD) / (VDD/256), VDD the corner's rail | LSB | -0.5 .. 0.5 | derived |
| t_settle_rise | code 0 -> 255, edge to vout staying within 0.5 LSB (VDD/512) of final | s | < 20e-9 | brief |
| t_settle_fall | code 255 -> 0, same | s | < 20e-9 | brief |
| t_rise_10_90 | 10-90 % rise time of the 0 -> 255 step | s | <= 20e-9 | derived (implied by settling) |
| t_fall_10_90 | 90-10 % fall time of the 255 -> 0 step | s | <= 20e-9 | derived (implied by settling) |
| glitch_energy_up | 127 -> 128, all inputs switching together: integral of abs(vout - v_final) dt outside the band [v_initial, v_final] | V.s | <= 0.26e-9 | derived |
| glitch_energy_down | 128 -> 127, same | V.s | <= 0.26e-9 | derived |
| glitch_peak_up | 127 -> 128, max excursion beyond that band | V | <= 52e-3 | derived |
| glitch_peak_down | 128 -> 127, same | V | <= 52e-3 | derived |
| glitch_energy_up_skew_p200p | 127 -> 128, MSB input 200 ps late | V.s | <= 0.5e-9 | derived |
| glitch_energy_up_skew_m200p | 127 -> 128, MSB input 200 ps early | V.s | <= 0.5e-9 | derived |
| glitch_energy_down_skew_p200p | 128 -> 127, MSB 200 ps late | V.s | <= 0.5e-9 | derived |
| glitch_energy_down_skew_m200p | 128 -> 127, MSB 200 ps early | V.s | <= 0.5e-9 | derived |
| glitch_peak_skew_max | max peak excursion over the four skewed cases | V | <= 0.2 | derived |
| inl_sens_1pct_max | worst max abs INL with any one unit resistor at +1 % or -1 % | LSB | <= 0.5 | derived (brief's INL target) |
| i_vdd_avg | average vdd current over the full-code sweep | A | <= 15e-3 | derived (brief's "~15 mA") |
| i_vdd_peak | peak vdd current over the full-code sweep | A | <= 30e-3 | derived |

How the derived bounds were set:

- v_zero_offset, fs_gain_err_lsb: the brief calls v(0) and v(255)'s gap
  to the ideal the offset and gain error and sets +-0.5 LSB as its
  accuracy target; the gain error is normalised to the corner's own VDD so
  it survives the +/-10 % supply corners.
- 10-90 % times: always shorter than settling to 0.5 LSB, so the brief's
  20 ns settling bound is the implied ceiling.
- Aligned glitch: a 1-LSB-band excursion (12.9 mV) lasting one 20 ns
  period is 0.26 nV.s; a 4-LSB peak is 52 mV.
- Skewed glitch: while the MSB lags by 200 ps the output heads toward a
  half-scale code, so the energy is about (VDD/2) x 200 ps = 0.33 nV.s
  independent of tau and the peak about (VDD/2)(1 - exp(-200 ps/tau)) ~
  0.12 V at tau = 2.7 ns; bounds are 1.5x those.
- inl_sens_1pct_max: a design whose INL holds +-0.5 LSB with any single
  unit 1 % off is what "ratio set by matching" means in practice.
- Supply current: the brief's own "a few to ~15 mA"; the peak adds the
  7 pF load's slew current through ~380 ohm (~9 mA) and rounds to 30 mA.

The brief's "typical" qualifier on the DNL/INL targets cannot be expressed
per corner (per-measure corners are lint-only), so dnl_max_abs and
inl_max_abs are scored at every corner; the brief itself expects them to
exceed 0.5 LSB at ss/125 C and ff/-40 C. That is a reported result for
the H1 checkpoint, not a bound to widen.

## Post-layout

After extraction (magic, R and C), the PEX bench re-runs the settling and
glitch measures at tt at least, to the same bounds (`post_layout_bounds`
in spec.yaml; the H2 reviewer compares them to layout_ref/*.bounds.json by
hand - nothing cross-checks them).

## Layout and checks (for P5, from the brief)

Layout is a generator, never a hand-drawn GDS: unit poly resistors common
centroid or at least interdigitated MSB/LSB, dummies at the array ends,
the eight driver cells in a row on the macro's own standard-cell rails
with vdd/vss straps, `dac_code[7:0]` pins on one edge, `vout` on another,
in the macro pin conventions the ade flow uses. netgen LVS clean against
the netlist including the driver cells; DRC clean; the macro must fit in
a fraction of the TT GF180 tile alongside the (tiny) digital front end.

## Round 2 revision (2026-09-25, owner decision)

- Settling: the owner kept the accurate 27k unit R and accepted ~1.2 us
  full-scale settling as a known trade-off against the brief's 20 ns. The
  bound is 1.5 us (tau = 27k x 7 pF = 189 ns; 9 ln2 tau = 1.18 us at tt,
  1.42 us at ss). 10-90 % rise/fall bound 0.5 us. The 20 ns figures above
  are the round-1 derivation at R = 250 ohm and no longer bind.
- Corners: the brief's nine, tt/ff/ss x -40/25/125 C at a fixed 3.3 V,
  now expressible as a corner grid (chip-flow #19).
