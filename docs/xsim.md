# xsim: a second simulator for the ade benches (#105)

`engine/scripts/xsim.py` runs a bench on ngspice and on Xyce at tt and
compares its measures. It runs on demand. It is not a gate: it records
nothing, and no gate reads it.

```
bin/eda python engine/scripts/xsim.py --workspace corpus/ade/ota
```

Which benches it runs: those with a `* xsim: true` comment line, in `tb/`
(a gated bench that is already plain SPICE) or in `xsim/` (a plain copy
written for the cross-check alone, kept out of `tb/` so the gates never see
it).

How close the engines must agree:
- 2% for a DC, AC, noise or transient value;
- 5% for a transient timing (TRIG/TARG, WHEN).

`--tol-dc` and `--tol-tran` change these. A measure that one engine gives
and the other fails or leaves out counts as a difference.

Exit codes: 0 when every marked bench agrees, 1 on a difference, 2 on an
error. A workspace with no marked bench is an error.

## Feasibility: do the PDK's ngspice model cards load in Xyce?

**Not unchanged.** Xyce stops on `libs.tech/ngspice/sm141064.spice` with
"Model is required for device M0", the first binned MOS model card. The
ngspice deck relies on ngspice's own binning and its parameter spellings.

The PDK ships a Xyce port, `libs.tech/xyce/design.xyce` and
`sm141064.xyce`. It has the same `.lib` section names (typical, ff, ss, fs,
sf, the res_*, cap_mim and moscap corners), and it loads. Against the ngspice
deck it differs in about 1160 lines:
- `.options model_binning`;
- `temper` is spelled `temp`;
- the BSIM4 instance lines drop `mulu0`, `nf`, `sa`, `sb` and `sd`;
- the `nplus_u`/`pplus_u` resistor parameters `lr`/`wr` become
  `r_length`/`r_width`;
- the `np_junction` diode is level 2 rather than 3;
- MIM parameters are quoted.

`agauss` and the `.lib` sections work as they are. Run through the port, a
single `nfet_03v3` operating point agrees with ngspice to 0.1%, so VACASK
with OpenVAF models was not needed.

The catch: xsim compares ngspice-on-the-ngspice-deck with
Xyce-on-the-Xyce-port. A difference can come from the port, not the engine.

## What makes a bench plain enough

xsim refuses a bench, and does not guess, when it has:
- a `.control` block that runs anything but `run` (or `set noaskquit`);
- a `.measure noise` (ngspice has none);
- more than one analysis (Xyce runs one per deck: split the bench);
- no analysis;
- a `$&` vector outside `.control`;
- a top-level `.param` defined twice. ngspice keeps the last definition and
  Xyce the first, so the engines would simulate different circuits.

## Translation

On ngspice: the bench as written, except the analysis line goes into the
`.control` block as a command. A dot `.ac` under a bare `run` in batch mode
prints its measures, then fails a second, empty AC pass ("no data saved for
A.C. Small signal analysis").

On Xyce:
- The `.control` block is dropped.
- The ngspice model decks become the Xyce port.
- `TRIG v(x) VAL=v` becomes `TRIG v(x)=v` (TARG too).
- The `{{SIZING}}` line moves to the top of the deck, so the first `.param`
  wins.
- Xyce reads `vp()` in degrees; xsim converts ngspice's radians, and wraps a
  phase difference modulo 360.

For a `.noise` bench, xsim adds `onoise_rms`, the rms output noise over the
`.noise` sweep:
- ngspice: `onoise_total` from the noise2 plot;
- Xyce: the square root of `.measure noise ... INTEG onoise`, which is in V².

## Results at tt (2026-11-08)

Known-answer RC (1 kOhm, 1 nF; `tests/fixtures/xsim`):

| measure | expected | ngspice | Xyce | difference |
|---|---|---|---|---|
| t50 | RC ln 2 = 693.15 ns | 693.15 ns | 693.40 ns | 0.04% |
| v(1 us) | 1 - 1/e = 0.63212 | 0.63212 | 0.63218 | 0.01% |
| f3db | 159.155 kHz | 159.18 kHz | 159.18 kHz | 0% |
| phase at f3db | -45 deg | -44.993 | -44.993 | 0% |
| onoise_rms, 10 Hz-1 MHz | 1.9307 uV | 1.9301 uV | 1.9323 uV | 0.12% |

The planted ngspice-only default: `c1 out 0 1n ic=1` with no `.ic` or
`uic`. ngspice drops the `ic=`, so v(10 ns) is 0; Xyce honours it and gives
0.990. xsim reports this as a difference, as it should. With `.ic v(out)=1`
instead, the two agree (0.99005 vs 0.99006).

Corpus:

| bench | measure | ngspice | Xyce | difference |
|---|---|---|---|---|
| mirror_tb (tb/) | iout_raw | 24.043 uA | 24.045 uA | 0.01% |
| ota_ol_xsim (stb) | a0_mag | 31396 | 31392 | 0.01% |
| | ugf_hz | 10.139 MHz | 10.142 MHz | 0.03% |
| | ph_1meg / ph_5meg | -91.99 / -100.02 deg | -91.99 / -100.02 deg | <0.01% |
| ota_psrr_xsim | psg_dc / psg_100k | 1.2669e-5 / 9.698e-3 | 1.2669e-5 / 9.694e-3 | 0.03% |
| ota_noise_xsim | onoise_rms | 33.895 uV | 33.921 uV | 0.08% |
| comparator_tb | tdelay | 358.13 ps | 359.31 ps | 0.3% (timing, 5%) |
| | itail_peak | 1.1772 mA | 1.1769 mA | 0.03% |
| comparator_offset_tb | vin_offset | -240.09 uV | -239.33 uV | 0.3% |

All agree. The comparator benches did not at first: at ngspice's default
step, min(tstep, span/50), itail_peak read 1.2989 mA against Xyce's
1.1887 mA (8.5%), and vin_offset -243.6 uV against -253.5 uV (3.9%). Both
differences came from the time step, not the models. The default step was
10 ps against a 50 ps clock edge, so the tail-current peak, taken during
the edge, was read 10% high.

Sweeping ngspice's max step on comparator_tb (Xyce at 1 ps: 1.1747 mA):

| max step | itail_peak | off |
|---|---|---|
| default (10 ps) | 1.2989 mA | 10.6% |
| 5 ps (edge/10) | 1.2160 mA | 3.5% |
| 2 ps (edge/25) | 1.1772 mA | 0.2% |
| 1 ps | 1.1755 mA | 0.07% |

The fix is in the benches, not the bounds. Both gated benches now give
their `.tran` a max step of the fastest edge over 25 (`.tran 10p 20n 0 2p`,
`.tran 20p 644n 0 2p`). The sim gates (sim_run, so sim_tt, sim_pvt and
netlist_lint) warn `tran_step_coarse` on a bench whose max step is coarser
than its fastest PULSE or PWL edge over 25
(`engine/lib/netlistlib.py:tran_step_coarse`). It is a warning, not an
error: a measure read long after the edge (a settled value, a slow
crossing) does not need the fine step, and only xsim can tell the two
apart. A tran line written with `$&` vectors from `.control` (the step
settling template) cannot be read and is not checked.

`tests/test_xsim.py` plants the comparator bench without its max step and
expects xsim to flag itail_peak.

## Xyce's resistor A_R (for #271)

The Xyce port's resistor mismatch (`A_R`, the Pelgrom term `check_mc.py`
carries into ngspice) does run in Xyce, with
`.sampling useexpr=true` and `.options samples numsamples=N
sample_type=mc`. Xyce writes one measure file per sample (`.ms0`,
`.ms1`, ...).

The catch: `design.xyce` sets `.param sw_stat_mismatch=0` (and
`sw_stat_global=0`) itself, and because Xyce keeps the first definition, a
later override does nothing. Run with the include untouched, all 200
samples come out identical. Set the switch inside a copy of `design.xyce`
and a `ppolyf_u` with r_length=10u and r_width=1u gives sigma/mean 0.43%
over 200 samples, against 0.47% from the A_R formula: within the sampling
error of a 200-run sigma.

## Not covered

- Only the tt corner.
- Only plain benches. None of the PLL's seven analog benches
  (blocks/pll/analog/tb, not marked here) qualifies as written: each runs
  its analyses, alters and loops from `.control`. Plain `xsim/` copies
  are within reach for startup (one tran), overlap (one tran per altered
  value: fix one), pwr (a dc sweep and a tran: split in two), presc (a tran
  and an op) and cp (op and dc points under `alter`: one small deck each).
  ol and vco loop over sweeps in `.control` and would need rewriting.
- The cross-check compares numbers, not waveforms.
