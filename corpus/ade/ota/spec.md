# ota - two-stage Miller OTA in unity-gain feedback

A rung of `/ade`'s corpus (docs/design.md section 3, "### M9."; issue
#100): the first block with a feedback loop, so the first whose spec owes a
stability bench, and the first laid out with every post-layout gate at
once - see "Layout" below.

## What it does

A two-stage operational transconductance amplifier on the 3.3 V devices of
gf180mcuD, used as a unity-gain buffer: `out` is tied to `inn`, so `out`
follows `inp`. Stage 1 is an NMOS differential pair with a PMOS mirror
load; stage 2 a PMOS common source with an NMOS sink. A Miller capacitor
across stage 2 splits the two poles so the loop is stable.
`skills/ade/reference/topologies/two_stage_miller_ota.sp` has the
equations.

## Interface

`two_stage_miller_ota(inp, inn, out, vdd, vss, ibias)`, in that pin order.
`inp` is the non-inverting input, `inn` the inverting one, `out` the
output, `vdd`/`vss` the rails (3.3 V), and `ibias` a pin that sinks the
bias current a bench forces into it (10 uA).

Use these ten devices, named exactly: the bias diode `xmb`; the tail
`xmt`; the input pair `xm1` (gate `inn`) and `xm2` (gate `inp`); the PMOS
mirror load `xm3` (diode) and `xm4`; the second stage `xm6` (PMOS) and its
sink `xm7`; the Miller cap `xcc`, a `cap_mim_2f0_m4m5_noshield`, and its
nulling resistor `xrz`, a `ppolyf_u` (about 4.6 kohm), in series from stage 1's output
(`d2`) through `xcc` and `xrz` to `out` (`xcc`'s bottom plate, `dz`, is the
node between them). Every transistor is a `nfet_03v3`
or `pfet_03v3`.

The four signal pins (`inp`, `inn`, `out`, `ibias`) go to the chip's
pads (spec.yaml's `ua_pins`), so every bench runs twice: bare (`no_pad`)
and through the pad model (`pad`, 500 ohm in series and 5 pF of pad
capacitance on each), and every bound holds in both.

## Requirement

Every bench runs the OTA as a unity-gain buffer with `inp` at mid-supply
(1.65 V), 10 uA into `ibias` and 2 pF from `out` to ground. At every
corner of docs/design.md section 5's default PVT set:

- `tb/ota_stb_tb.cir` (from `skills/ade/templates/stb_tb.cir`): the loop
  gain by Tian's method, the loop never broken.
  - `pm_deg`, the phase margin, is at least 60 degrees;
  - `gm_db`, the gain margin, is at least 10 dB;
  - `ugf_hz`, the loop's unity-gain frequency, is at least 5 MHz;
  - `a0_db`, the loop gain at 10 Hz, is at least 80 dB;
  - `iq`, the current from `vdd` (the bias current excluded), is
    150-350 uA;
  - `vos`, `v(out) - v(inp)` at DC, the systematic offset, is within
    +-1 mV.
- `tb/ota_psrr_tb.cir` (from `psrr_tb.cir`): 1 V AC on `vdd`.
  - `psrr_dc_db`, at 10 Hz, is at least 80 dB;
  - `psrr_at_f_db`, at 100 kHz, is at least 30 dB.
- `tb/ota_noise_tb.cir` (from `noise_tb.cir`): `onoise_rms_v`, the output
  noise integrated from 10 Hz to 1 MHz, is at most 60 uV rms. The bench
  prints each device's share after the measures, unscored.

Simulated values at the committed sizing across the seven default corners,
bare / through the pad: PM 87.4-104.2 / 75.3-92.6 deg, GM 22.5-28.2 /
11.3-21.4 dB, UGF 11.8-23.5 / 5.9-10.3 MHz; in both, A0 88-91 dB, iq
221-239 uA, vos -8 to -17 uV, PSRR 94.7-100.6 dB at 10 Hz and 40.8-46.7 dB
at 100 kHz, noise 30-39 uV rms. GM through the pad is the bound with least
room: tt_pff sets it (11.3 dB), and UGF through the pad at ss (5.9 MHz)
is next.

## Compensation and its planted faults

`xcc` is what makes the loop stable, and `xrz` in series with it puts the
zero that Cc alone would leave in the right half plane into the left one,
which is what keeps the loop stable through the pad's 5 pF. The two plants
in `faults/manifest.yaml` are on the cap: removing it fails `pm_deg` at
typical (`sim_tt`), and a cap too small (`cc_w` 22 um instead of 26 um,
about 0.97 pF instead of 1.35 pF) still meets every bound at typical but
loses gain margin through the pad at the fast corners (`sim_pvt`).
`bench_strength` mutates the cap too: it halves it (`cap_halved`, PM down
about 23%) and removes it (`cap_removed`); both are killed by the stability
bench, the first through `pm_deg`'s declared 5% sensitivity in
`tb/ota_stb_tb.bounds.json` (the corner spread of PM is about +-2.5%). The
tail with its width doubled stays inside every bound, its PM within 1%, but
its UGF rises about 79%, so `ugf_hz` declares a 10% sensitivity there.

## Layout

`layout/gen_ota.py` draws it through layoutlib (its docstring has the
floorplan): every transistor as equal one-finger units in four rows, the
input pair (xm1/xm2) and the mirror load (xm3/xm4) each a `matched_row`
drawn ABBA, the bias mirror (xmb/xmt/xm7) one common-centroid
`matched_row`, dummies at both ends of each (`xmdum1`-`xmdum6` in
`netlist/ota.cir`, every terminal on the row's rail). spec.yaml's
`matching:` names the three groups, and xcc is a 26 um MIM-B on
metal4/metal5 with xrz, a 2 x 25.5 um ppolyf_u, below it.

The post-layout set it passes: `drc`, `lvs`, `erc` (with `erc_nets` for
ibias, which the bench drives with a current), `layout_match`, `em_ir`
(bench `layout_ref/ota_emir_tb.cir`, `ir_drop_mv: 2`) and `pex_sim`
(bench `layout_ref/ota_pex_tb.cir`: the stability, PSRR and noise
benches on the extracted netlist at every corner, `pex_corners: all`,
with the kpex cross-check). One via1 cut carries 0.18 mA, so every stub's
via onto a track is a column of cuts across the track's width.

`faults/manifest.yaml` still has no layout-gate entries: this rung's
plants are the compensation faults above.

