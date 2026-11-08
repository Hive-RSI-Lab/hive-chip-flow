# ota - two-stage Miller OTA in unity-gain feedback

A rung of `/ade`'s corpus (docs/design.md section 3, "### M9."; issue
#100): the first block with a feedback loop, so the first whose spec owes a
stability bench. Like `bandgap`, its row may stay red at layout - see
"Layout" below.

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

Use these nine devices, named exactly: the bias diode `xmb`; the tail
`xmt`; the input pair `xm1` (gate `inn`) and `xm2` (gate `inp`); the PMOS
mirror load `xm3` (diode) and `xm4`; the second stage `xm6` (PMOS) and its
sink `xm7`; and the Miller cap `xcc`, a `cap_mim_2f0_m4m5_noshield` from
stage 1's output to `out`. Every transistor is a `nfet_03v3` or
`pfet_03v3`.

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

Simulated values at the committed sizing across the seven default corners:
PM 68.3-71.4 deg, GM 15.2-16.8 dB, UGF 7.2-14.1 MHz, A0 88-91 dB, iq
221-239 uA, vos -8 to -17 uV, PSRR 94.7-100.6 dB at 10 Hz and 37.3-43.2 dB
at 100 kHz, noise 30-39 uV rms. PM is the bound with least room: the fast
corners (ff, fs, tt_pff) set it.

## Compensation and its planted faults

`xcc` is what makes the loop stable, so the two plants in
`faults/manifest.yaml` are on it: removing it fails `pm_deg` at typical
(`sim_tt`), and a cap too small (`cc_w` 24 um instead of 32 um, about
1.15 pF instead of 2.05 pF) still meets 60 degrees at typical but not at
the fast corners (`sim_pvt`). `bench_strength` mutates the cap too: it
halves it (`cap_halved`, PM about 42 degrees) and removes it
(`cap_removed`); both are killed by the stability bench. The tail with its
width doubled stays inside every bound (PM falls to about 60.4), so
`pm_deg` declares a 5% sensitivity in `tb/ota_stb_tb.bounds.json` (the
corner spread of PM is about +-2.5%).

## Layout

No `layout/` generator, and no `drc`/`lvs`/`pex_sim`/`release` entries in
`faults/manifest.yaml`. Issue #100 is about the stability, PSRR and noise
benches and the gates that check them; a laid-out OTA (matched pair,
common-centroid mirror, the MIM cap on metal 4/5) is a rung of its own.
This spec covers what `spec.yaml` checks pre-layout: `spec_lint`,
`netlist_lint`, `sim_tt`, `sim_pvt`, `bench_strength`.
