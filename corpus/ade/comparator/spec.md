# comparator - clocked StrongARM latch comparator

The second rung of `/ade`'s corpus (docs/design.md, "### M9."): a clocked
comparator with no static current, taken from spec to a released layout.

## What it does

A dynamic comparator on the 3.3 V devices of gf180mcuD. While `clk` is low
it resets: both outputs are pulled to `vdd` and it draws no static current.
When `clk` rises it compares `inp` with `inn` and regenerates the difference
to full logic levels: `outp` goes high and `outn` low when `inp > inn`, and
the other way round when `inn > inp`. The decision holds until `clk` falls.

## Interface

`strongarm_comparator(inp, inn, clk, outp, outn, vdd, vss)`, in that pin
order. `inp`/`inn` are the differential inputs, `clk` the clock, `outp`/
`outn` the complementary outputs, `vdd`/`vss` the rails. Supply 3.3 V.

Use the StrongARM latch topology with these eleven devices, named exactly:
a clocked NMOS tail `xmtail`; the NMOS input pair `xmip` (gate `inp`) and
`xmin` (gate `inn`); the cross-coupled latch NMOS `xmln1`, `xmln2` and PMOS
`xmlp1`, `xmlp2`; and four clocked PMOS reset switches `xmrp`, `xmrn` (the
input pair's drains) and `xmrop`, `xmron` (the outputs). Every device is a
`nfet_03v3` or `pfet_03v3`.

## Requirement

The bench: `clk` a 0-to-`vdd` pulse, 10 ns period, rising at 5 ns and 15 ns
with 50 ps edges and about 5 ns high. The inputs sit at mid-supply
(1.65 V) with `inp` 1 mV above `inn` for the first decision; they swap
during the reset at 11 ns, so `inn` is 1 mV above `inp` for the second. Each
output carries a 10 fF load to ground. At every corner of docs/design.md
section 5's default PVT set:

- `vdiff_pos`: `v(outp) - v(outn)` at 9.5 ns (late in the first decision)
  is at least 2.5 V.
- `vdiff_neg`: the same difference at 19.5 ns (the second decision) is at
  most -2.5 V.
- `tdelay`: from `clk` rising through 1.4 V to `outn` falling through
  1.4 V, first decision, is between 0.1 ns and 0.6 ns.
- `vreset`: `v(outn)` at 14.5 ns, in the reset between the decisions, is at
  least 2.6 V.
- `itail_peak`: the peak current through the comparator's `vss` pin
  between 5 ns and 7 ns (the first evaluation, clk high) is between
  0.7 mA and 2.0 mA. Only the tail's source returns to `vss`, so this is
  the tail current, and the tail's width sets it. The four measures above
  cannot see the tail's size: a 1 mV input still resolves to the rails
  within 0.6 ns with the tail twice as wide, so without this bound a
  wrongly sized tail passes. Real simulated values (typical/ss/ff/sf/fs):
  1.30, 1.07, 1.61, 1.06, 1.57 mA; the tail at twice its width draws
  2.34 mA at typical.

These five are nominal checks: sim_tt and sim_pvt score them with no
mismatch, and spec.yaml marks them `statistical: none`. Under device
mismatch a StrongARM's trip point moves by about 10 mV, so a 1 mV decision
says nothing about any single die. The offset bench below bounds that
spread.

### Input offset

The bench (`tb/comparator_offset_tb.cir`) uses the same clock, a 4 ns
period here, and the same loads. `inp - inn` ramps around mid-supply from
-80 mV to +80 mV, 1 mV per clock cycle. Below its trip point each decision
pulls `outp` low; above it, `outn`.

- `vin_offset`: the input at the first decision that pulls `outn` below
  1.4 V, less half a ramp step (1 mV resolution). It lies between -60 mV
  and +60 mV at every corner and in every Monte Carlo sample. A trip point
  outside the ramp leaves it unmeasured or pinned at the ramp's start,
  which fails the sample.
- Its Monte Carlo 1-sigma (`sigma_max`) is at most 20 mV at the typical
  corner and at the worst PVT corner. That holds for the sample sigma and
  for its chi-square upper bound at the gate's confidence. The ±60 mV
  bound is 3 x this sigma.

Monte Carlo is on (`mc: {enabled: true, yield_min: 0.95}`): the PDK's own
mismatch models, with yield proven at 95% confidence (docs/design.md
section 5). With no mismatch, `vin_offset` is -0.36 mV at typical. Real
Monte Carlo on this sizing (144 runs, seed 1): sigma 12.5 mV at typical
(upper bound 14.2 mV) and 12.0 mV at the worst corner, ff (upper bound
13.6 mV); every sample is inside ±60 mV. This is offset without
cancellation. A tighter offset needs a different topology (a preamp or
offset storage), which this block does not have.

After layout, at the typical corner, the extracted netlist with its wire
capacitance and resistance runs the same bench with a 10 mV input step in
place of 1 mV: both decisions still at least 2.5 V apart with the right
sign, the reset level at least 2.6 V, and `tdelay` between 0.1 ns and
0.5 ns. It does not bound `itail_peak`: wire parasitics do not move the
tail's own current, and the tail's size is what LVS checks. The step is larger because wiring parasitics give a latch a few mV
of offset (about 0.1 fF of difference between outp and outn is enough to
flip a 1 mV decision), so a 1 mV post-layout bench would test the extractor's
rounding, not the layout.

Real simulated `tdelay` on this box (typical/ss/ff/sf/fs): 0.357, 0.494,
0.268, 0.474, 0.288 ns; the outputs reach the rails at every corner.
