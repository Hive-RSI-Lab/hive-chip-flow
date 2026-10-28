# r2r_dac_analog - the R-2R ladder macro of r2r_dac

The analog side of the `/msde` block `r2r_dac` (../../brief/spec.md): an
8-bit R-2R ladder in GF180 (gf180mcuD) poly resistors, switched by eight
gf180mcu_fd_sc_mcu7t5v0 standard-cell drivers at 3.3 V, whose output
leaves the Tiny Tapeout tile on analog pin `ua[0]`. It is a hard macro
inside the digital tile, built and checked as a standalone `/ade` block:
netlist as code, layout as a generator, ngspice for every number, netgen
LVS clean.

## What it does

`vout = dac_code / 256 * VDD` (ideal), VDD = 3.3 V, so 1 LSB = 12.9 mV
and full scale is 255/256 x 3.3 V = 3.287 V.

- Eight switch drivers: one gf180mcu_fd_sc_mcu7t5v0 buffer cell per bit
  (pick one drive strength, the same cell for all eight bits - e.g.
  `gf180mcu_fd_sc_mcu7t5v0__buf_16` or the largest available, whichever
  gives the lowest on-resistance; state the measured pull-up and
  pull-down Ron at tt/25 C in the spec). Each driver's output drives the
  2R branch of its bit; its input is the crossing signal `dac_code[k]`.
  The drivers live in this macro, not on the digital side, so the ladder
  and the driver on-resistance are one accuracy budget that this side's
  bench measures directly, and so netgen LVS covers the whole signal
  path. The driver cells are instantiated from the PDK's cell spice
  library and placed in the layout generator from the PDK's cell GDS, on
  the macro's own vdd/vss; they are the only active devices in the macro.
- Ladder: standard R-2R, 8 rungs, terminated with 2R to vss at the LSB
  end. Poly resistors from the GF180 PDK (the high-sheet-resistance poly
  resistor, `ppolyf_u` or equivalent - state which). Build every R as a
  unit element and every 2R as two units in series, same width and
  length everywhere, so the ratio is set by matching, not by absolute
  value.
- Driver on-resistance absorption: the driver's Ron (tens of ohms) is in
  series with each 2R branch. Make the drawn 2R branch 2R - Ron_nom
  (Ron_nom = the average of pull-up and pull-down Ron at tt/25 C, in the
  same units as the ladder resistors, i.e. trim by a fraction of a unit
  or by the unit's length), so the tt/25 C INL meets the +-0.5 LSB
  target. The residual across PVT (Ron moves +-30 % while poly R moves
  much less, and pull-up and pull-down Ron differ) is what the corner
  report shows - it is expected to exceed +-0.5 LSB at ss/125 C and
  ff/-40 C; report it, do not hide it.

## Values chosen (state these in spec.yaml; revise with numbers)

- Pad load assumed: the TT GF180 analog pin ua[0] is modelled as
  C_pad = 5 pF to vss (pad + ESD + package) plus 2 pF for the tile-to-pad
  routing, in series with R_pad = 100 ohm (pad/ESD series resistance),
  i.e. R_pad = 100 ohm, C_load = 7 pF. This is an assumption - the TT
  analog pad's actual load is not documented in this repo; the spec must
  say so and the bench must have it as one parameter so it can be changed.
- R = 250 ohm (2R = 500 ohm). The ladder's Thevenin output resistance is
  R plus a share of the driver Ron (about Ron/2 across codes), so
  tau = (250 + ~30 + 100) x 7 pF = 2.7 ns. Settling to 0.5 LSB of 8 bits
  is ln(512) = 6.24 tau = 17 ns, inside one 50 MHz period (20 ns) at tt;
  at ss/125 C with poly R +20 % and Ron +30 % it is ~19 ns, still inside.
  Cost: the drivers see ~3R = 750 ohm, up to 4.4 mA per bit, and the
  termination draws 6.6 mA at full scale; total supply current is a few
  to ~15 mA, acceptable for a TT tile on its own 3.3 V rail.
  If the measured settling misses 20 ns at the worst corner, lower R
  (200 ohm) before anything else; if the current budget is the problem,
  raise R and report the settling as multiple periods - both go to the
  owner as OPEN, not silently.
- Unit resistor: choose width for mismatch, not minimum - a poly unit of
  about W = 2 um at the length that gives R = 250 ohm (or the nearest
  the PDK's sheet resistance allows; if 250 ohm needs a length below the
  PDK's minimum, use 2 units in parallel per R and 4 per 2R with 2 in
  series). State the drawn W, L, and the expected sigma(R)/R from the PDK
  mismatch data if it exists.

## Interface (.subckt pins)

`r2r_dac_analog(dac_code[7] dac_code[6] dac_code[5] dac_code[4] dac_code[3]
dac_code[2] dac_code[1] dac_code[0] vout vdd vss)`

- `dac_code[7:0]` - the crossing signal ../../interface.yaml names:
  direction d2a, level cmos_3v3, domain clk_sys, width 8. Eight CMOS
  inputs, each the input of one driver cell; the digital side drives
  them from a register clocked at 50 MHz. Bit 7 is the MSB.
- `vout` - the ladder output. NOT a crossing signal: it does not go to the
  digital side. It leaves the tile on Tiny Tapeout analog pin `ua[0]`
  (the one analog pin the brief allows). At `top_harden` it is routed
  from the macro pin to the tile's `ua[0]` connection; nothing in the
  digital netlist touches it.
- `vdd` / `vss` - the single 3.3 V supply and ground, shared with the
  digital tile. Exactly one supply and one ground pin.

If the nested flow or `top_harden` cannot express a macro pin that goes
to a tile pad rather than to the digital side, stop and report that;
do not fold `vout` into the digital side.

## Quantify (ngspice, this side's bench)

All measures at the ladder output into the assumed pad load, across
process tt / ff / ss and temperature -40 / 25 / 125 C (nine corners; add
the sf/fs corners if the PDK's poly-resistor corner files give them),
VDD 3.3 V. Report tt/25 C as "typical" and the worst corner for each.

1. DNL: sweep all 256 codes (hold each code long enough to settle,
   e.g. 200 ns), measure vout at the end of each step; DNL(k) =
   (v(k) - v(k-1)) / LSB_measured - 1, with LSB_measured = (v(255) -
   v(0)) / 255 (end-point fit). Report max |DNL|. Target +-0.5 LSB
   typical.
2. INL: same sweep, end-point fit line; INL(k) = (v(k) - fit(k)) /
   LSB_measured. Report max |INL|. Target +-0.5 LSB typical.
3. Monotonicity: v(k) > v(k-1) for all k = 1..255 (equivalently DNL >
   -1 everywhere). Pass/fail per corner.
4. Full-scale range: v(0) and v(255); report v(255) - v(0) and compare
   with the ideal 255/256 x 3.3 V = 3.287 V. Also v(0) itself
   (zero-code offset from vss) and v(255)'s gap to vdd - both come from
   driver Ron and are the gain/offset error.
5. Full-scale settling: step the code 0 -> 255 and 255 -> 0; time from
   the code edge until vout stays within 0.5 LSB (6.45 mV) of its final
   value. Must be < 20 ns at every corner (one 50 MHz period). Also
   report the 10-90 % rise/fall time.
6. Mid-code glitch energy: step 127 -> 128 and 128 -> 127 (all 8 bits
   switch, the worst code transition for an R-2R) with the driver
   inputs switching at the same instant, and separately with the MSB
   input skewed +-200 ps against the others (the digital side's
   registered output can have that much output skew). Glitch energy =
   integral of |vout(t) - v_final| dt over the transient, in nV.s,
   excluding the ideal 1-LSB step (integrate the overshoot beyond the
   band between v_initial and v_final). Report the peak glitch amplitude
   too.
7. Monte-Carlo resistor mismatch: if the GF180 PDK ngspice models carry
   a mismatch / statistical section for the poly resistor used (check
   the PDK's model files for a `mc` or `mismatch` library section on
   the resistor), run >= 200 samples of the DNL/INL sweep at tt/25 C
   and report the mean and 3-sigma of max |DNL| and max |INL| and the
   fraction of samples that stay monotonic. If the models do not
   support it, say so explicitly in the report and instead run a
   deterministic sensitivity: perturb each unit resistor by +-1 % one at
   a time and report the worst INL that produces.
8. Supply current: the average and peak vdd current over the full-code
   sweep, so the top level knows the tile's draw.

## Layout and checks

- Layout is a generator (gdsfactory or the ade flow's own generator
  convention), never a hand-drawn GDS: unit poly resistors in a common
  centroid or at least interdigitated MSB/LSB arrangement, dummies at
  the array ends, the eight driver cells in a row on the macro's own
  standard-cell rails with vdd/vss straps, `dac_code[7:0]` pins on one
  edge, `vout` on another, in the macro pin conventions the ade flow
  uses (see corpus/msde/sensor_counted/analog for the shape).
- netgen LVS clean against the netlist (including the driver cells).
- Magic DRC clean; the macro must fit in a fraction of the TT GF180
  tile with the digital front end (the front end is tiny).
- PEX bench (after extraction) re-runs measures 5 and 6 at tt/25 C at
  least.

## Not on this side

- The code source (register, ramp, sine) is the digital side
  (../../digital/brief/spec.md). This side's bench drives `dac_code`
  itself as ideal 3.3 V piecewise-linear sources with 100 ps edges.
- Code-to-voltage measures across the whole tile (the ramp from the
  digital side producing the staircase at ua[0]) are top-level cosim
  measures.
