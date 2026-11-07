# sar_adc

An 8-bit SAR (successive-approximation) ADC (docs/design.md section 3's
`/msde` corpus list, "### M10.": "the SAR ADC join[s] as specs with faults
and may stay red").

**This rung is a spec, faults and a cosim bench.** There is no tile
design yet (no nested `/vde`/`/ade` workspaces), so `split` and `cosim`
run here (docs/design.md 1.5 msde table). `cosim` runs a bench-only rung,
as `ring_osc_div` does: the SAR logic in `tb/sar_adc_digital.v` against a
behavioral analog side, `tb/sar_adc_ideal.sp`. Its `ladder.md` row may
stay red until a later milestone gives it a tile and wires up `top_drc`,
`top_lvs` and `release`.

## What the block does

The digital side is the SAR control logic: `clk` and `start` (TT pins) run
a successive-approximation search that drives `dac_code` one bit at a time,
reads back `cmp_out` after each step, and on completion raises `done` with
the final 8-bit `result`. The analog side is a sample-and-hold that closes
on `sample`, a capacitive (or R2R) DAC that converts `dac_code` to a trial
voltage, and a comparator whose output is `cmp_out`.

## Crossing signals

| signal    | direction | level     | domain   | width |
|-----------|-----------|-----------|----------|-------|
| dac_code  | d2a       | cmos_3v3  | clk_sys  | 8     |
| sample    | d2a       | cmos_3v3  | clk_sys  | 1     |
| cmp_out   | a2d       | cmos_3v3  | clk_sys  | 1     |
| comp_clk  | d2a       | cmos_3v3  | clk_sys  | 1     |

## Dynamic measures (the cosim bench)

`tb/test_sar_adc_cosim.py` runs the SAR back to back on a sine input and
scores the codes with `engine/lib/convlib.py` (#167):

- The SAR takes 20 clocks per conversion (4 to track, 2 per bit) at a
  50 ns clock, so fs = 1 MHz. The input is 1.65 V + 1.6 V sin(2 pi fin t),
  with fin = 46875 Hz, the `.param fin` in `tb/sar_adc_ideal.sp`. That is
  bin 3 of a 64-point record, coherent and coprime; convlib refuses any
  other fin, and the bench then reports no `enob`.
- One warm-up conversion is discarded, then 64 codes are scored.
- Bounds (`tb/sar_adc.bounds.json`): `conversions` >= 64 and `enob` >= 7.5.
  The ideal model gives ~7.9. The plant `cosim_dac_msb_weight_low_enob`
  makes the MSB weigh 3% more (a capacitor mismatch), which gives ~6.3 and
  fails `measure_out_of_bounds`. `sndr`, `snr`, `sfdr` and `thd` are
  reported but not bounded.

## Static measures (not yet run)

A bench on a real analog netlist should also check, at gf180 3.3 V supply:

- A DC input at 0 V, 1.65 V and 3.2 V each converts to within ±1 LSB
  (12.9 mV at 3.3 V full scale / 256 codes) of the expected 8-bit code.
- Conversion time: `start` to `done` completes within 8 `comp_clk` cycles
  (one bit decision per cycle) plus one sample-and-hold settling cycle.
- `done` stays low until the 8th bit decision latches, and `result` holds
  steady until the next `start`.

These are recorded as the contract a tile design would have to meet.
