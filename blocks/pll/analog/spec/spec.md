# pll_analog - analog side of the charge-pump PLL (spec)

Written from `brief/spec.md` alone (the reviewed proposal is the brief's
source, not re-read here). GF180MCU, 3.3 V, a hard macro inside the Tiny
Tapeout GF180 analog-tile top `tt_um_ihsan_sa_pll`. The machine-checkable
half is `spec/spec.yaml`; measure names there are load-bearing.

## Behaviour

The cell is the charge pump, the passive loop filter, the current-starved
ring VCO and their bias. PFD, /8 prescaler, /N divider, lock detector and
control decoding are digital and outside this cell.

1. **Charge pump.** Switched PMOS (UP) and NMOS (DN) current sources from a
   mirror off the bias. `pfd_up` = 1 sources Icp into `vctrl`; `pfd_dn` = 1
   sinks Icp; both low holds the node; both high (PFD reset overlap, about
   1 ns) nets as close to zero charge as matching allows. Trim codes
   `cp_trim1:0` select the mirror ratio. Chosen steps (mirror units
   4:5:6:8):

   | code (trim1 trim0) | Icp nominal |
   |---|---|
   | 00 | 20 uA |
   | 01 | 25 uA |
   | 10 | 30 uA |
   | 11 | 40 uA (2x, the fix for low damping at P*N = 32-40) |

   Concerns: UP/DN mismatch against `vctrl` over the tuning range (static
   phase offset, reference spur), charge sharing at switching. A unity-gain
   buffer equalising the switch nodes is added only if ngspice shows the
   overlap charge or mismatch bound is missed without it.
2. **Loop filter.** Series R1-C1 with shunt C2 on `vctrl`: R1 about
   22 kOhm, C1 about 20 pF, C2 about 2 pF (estimate), for fn about 400 kHz
   and zeta about 0.55 at P*N = 16 with Kvco about 2*pi*100 MHz/V. `vctrl`
   goes to pad `ua[0]`, so up to 5 pF of pad sits in parallel with C2; C2 is
   sized with the pad included. C1 (20 pF) is the area risk: if it does not
   fit the footprint, that is raised in OPEN, never shrunk silently
   (capacitance at `ua[0]` adds to C2, not C1).
3. **VCO.** Current-starved inverter ring, odd stage count from the ngspice
   sweep; `vctrl` sets the starving current. Positive Kvco (higher `vctrl`,
   higher frequency - the digital PFD polarity assumes this), monotonic at
   every corner, about 50-200 MHz at typical, up to about 363 MHz at the
   fast/cold/low-supply corner with `vctrl` at the rail (370 MHz ceiling,
   the 2.7 ns the digital side is timed at), Kvco about 100 MHz/V (estimate). Output
   buffered to a full-swing 3.3 V CMOS clock `vco_out` driving one `dffq_1`
   clock pin plus one buffer (about 15 fF) with rise/fall <= 0.15 ns at
   333 MHz (the Liberty condition the prescaler was checked at).
4. **Bias.** One reference current for pump mirror and VCO. `bias_ref` goes
   to pad `ua[1]` for an optional external resistor to ground; the cell must
   work with `ua[1]` floating (internal default bias) and survive probe /
   ESD-class capacitance on it.
5. **Power-down.** `pll_en` = 0 turns off pump, bias and VCO (`vco_out` held
   static, no oscillation, standby current reported); `pll_en` = 1 enables
   them. Quasi-static input.

## Interface

Supply: one `vdd` at 3.3 V nominal (+-10 % over corners), one `vss`. The
`.subckt pll_analog` pins are exactly the six `interface.yaml` signals, the
two `ua_pins`, `vdd` and `vss` - ten pins, no other. Renaming, widening or
adding one goes back through the msde `split` step.

| Pin | Dir | Level | Domain | Meaning |
|---|---|---|---|---|
| `vco_out` | a2d out | cmos_3v3 | clk_free | buffered VCO clock to the /8 prescaler, up to 370 MHz |
| `pfd_up` | d2a in | cmos_3v3 | clk_ref | UP pulse, active high, source Icp into `vctrl` |
| `pfd_dn` | d2a in | cmos_3v3 | clk_fb | DN pulse, active high, sink Icp from `vctrl` |
| `pll_en` | d2a in | cmos_3v3 | clk_ref | 1 = run, 0 = pump, bias, VCO off |
| `cp_trim0` | d2a in | cmos_3v3 | clk_ref | pump trim bit 0 |
| `cp_trim1` | d2a in | cmos_3v3 | clk_ref | pump trim bit 1 |
| `vctrl` | analog pad | analog | - | loop filter node, pad `ua[0]` (up to 5 pF) |
| `bias_ref` | analog pad | analog | - | optional external bias resistor node, pad `ua[1]` |
| `vdd` | supply | 3.3 V | - | the one supply pin |
| `vss` | ground | 0 V | - | the one ground pin |

Digital inputs present a CMOS-gate load (no DC current); the inversion for
the PMOS UP switch is inside the cell. `vco_out` is the only logic output.
`vctrl` and `bias_ref` go straight to the pads and never to the digital
side. No split (standard-cell) devices live in this macro.

### Corners

The brief asks for typ/fast/slow process, supply +-10 %, -40 to 125 C:
`spec.yaml` declares the grid {typical, ss, ff} x {-40, 27, 125} C x
{-10, 0, +10} % = 27 corners, plus the transistor/resistor skew corners
`ss_pff` (ss, 125 C, -10 %, fast R and C) and `ff_pss` (ff, -40 C, +10 %,
slow R and C) through the grid's `extra`. The engine appends `tt_pss` and
`tt_pff` (typical transistors at each RC extreme) because the netlist uses
poly resistors and MIM caps: 31 corners. Names follow `corners.py`: `tt_27c`,
`ss_125c_vm10`, `ff_m40c_vp10` (the brief's ff_n40C_3v60), etc.

### Measures (ngspice)

Conditions unless stated: `pll_en` = 1, `cp_trim` = 00, `bias_ref`
floating (internal default), `vco_out` loaded with 15 fF. The `vctrl`
tuning range for the pump measures is 0.3 V to (vdd - 0.3 V): the brief's
"0.3-3.0 V" cannot be met at vdd - 10 % = 2.97 V, so the upper end tracks
the rail. Rise/fall are 20 %-80 %. "all" = all 31 corners.

| Measure | Bound | Corners | What it is |
|---|---|---|---|
| `icp_up_code00_ua` | 12..30 | all | UP source current, mid-range `vctrl` (vdd/2), code 00 (20 uA nominal, -40/+50 % over PVT and resistor skew) |
| `icp_dn_code00_ua` | 12..30 | all | DN sink current, same conditions |
| `icp_up_code00_tt_ua` | 18..22 | tt_27c | code 00 nominal check (20 uA +-10 %) |
| `icp_up_code01_ua` | 15..37.5 | all | code 01 (25 uA nominal, -40/+50 % over PVT and resistor skew) |
| `icp_up_code10_ua` | 18..45 | all | code 10 (30 uA nominal, -40/+50 %) |
| `icp_up_code11_ua` | 24..60 | all | code 11 (40 uA nominal, -40/+50 %) |
| `icp_code11_over_code00_ratio` | 1.8..2.2 | all | the codes reach 2x |
| `cp_updn_mismatch_pct` | <= 10 | all | max of 100*abs(Iup-Idn)/Icp over the `vctrl` range (systematic; GF180 models carry no per-instance mismatch) |
| `cp_overlap_net_charge_fc` | <= 10 | all | abs net charge into `vctrl` per event with both inputs high for 1 ns, incl. charge sharing |
| `vctrl_leak_off_na` | <= 1 | all | abs current into `vctrl` with both inputs low, over the range |
| `ol_pm_pn16_nopad_deg` | >= 45 | tt_27c | open-loop phase margin, pump-filter-VCO with ideal 1/16 divider, no pad |
| `ol_pm_pn16_nopad_pvt_deg` | >= 30 | all | same definition as `ol_pm_pn16_nopad_deg`, at every corner: loop gain Icp*Kvco moves ~4.6x over PVT and resistor skew, and 30 deg is the floor across corners |
| `ol_pm_pn16_pad5p_deg` | >= 25 (warn) | tt_27c | same with 5 pF on `vctrl` (reported; brief estimate 32) |
| `ol_fc_pn16_nopad_khz` | 200..1000 (warn) | tt_27c | unity-gain crossover, P*N = 16, no pad |
| `ol_pm_pn8_nopad_deg` | >= 30 (warn) | tt_27c | phase margin at P*N = 8 |
| `ol_pm_pn40_nopad_deg` | >= 30 (warn) | tt_27c | phase margin at P*N = 40 |
| `vco_kvco_mhz_per_v` | 40..250 | all | mean df/dv over the tuning range |
| `vco_df_dv_min_mhz_per_v` | >= 0 | all | minimum df/dv step of the sweep: monotonic, positive Kvco |
| `vco_fmin_tt_mhz` | <= 50 | tt_27c | frequency at the low end of the range |
| `vco_fmax_tt_mhz` | >= 200 | tt_27c | frequency at the high end of the range |
| `vco_fmax_ff_mhz` | 150..370 | all | frequency at `vctrl` = vdd: the VCO's ceiling, held under the 370 MHz (2.7 ns) the digital timing is closed at, and over the 150 MHz lock point every corner keeps |
| `vco_out_swing_pct_vdd` | >= 90 | all | (Vhigh - Vlow)/vdd on `vco_out` into 15 fF |
| `vco_out_tr_ns` | <= 0.15 | ff_m40c_vp10, tt_27c | rise 20-80 % at maximum frequency into 15 fF |
| `vco_out_tf_ns` | <= 0.15 | ff_m40c_vp10, tt_27c | fall, same |
| `vco_out_duty_pct` | 40..60 | all | duty at mid-range `vctrl` |
| `presc_div2_ratio` | 0.99..1.01 | ff_m40c_vp10 | `vco_out` driving the extracted `gf180mcu_fd_sc_mcu7t5v0__dffq_1` + `inv_1` toggle stage: f_toggle / (f_vco/2) |
| `standby_current_ua` | <= 1 | all | vdd current with `pll_en` = 0 |
| `vco_out_off_toggles` | 0 | all | `vco_out` transitions with `pll_en` = 0 (held static) |
| `startup_time_us` | <= 5 | all | `pll_en` rise to a running, full-swing `vco_out`, with 5 pF on `vctrl` and on `bias_ref` |

Post-layout (`post_layout_bounds`): `icp_up_code00_ua`, `icp_dn_code00_ua`
(12..30), `vco_kvco_mhz_per_v` and `vco_fmax_tt_mhz` (>= 200, added
2026-10-28 after the first extraction read 154.8 MHz) re-simulated from the
extracted netlist at typical hold the same bounds. No Monte Carlo: the brief asks for no yield number, and GF180
poly models have no per-instance mismatch to draw from anyway.

### Revision 2026-10-10 (H1)

The charge-pump bias is Vgs/R self-biased, so the worst Icp is not at the
grid's ss/ff corners (slow transistors partly cancel slow resistors) but at
transistor/resistor skews: a scratch `sim_run` at code 00 read 14.25 uA
(ff_pss) to 27.06 uA (ss_pff), about 12.3..29.2 uA with temperature
extrapolated, against 15.09..24.68 uA over the 27-point grid. The range is
already centred on ~20 uA, so re-centring the bias buys nothing; the old
+-25 % windows were a proxy for loop gain, which moves ~4.6x (Kvco
49.7..115 MHz/V), so the Icp windows were widened to the skew spread plus
margin and the real requirement, a stable loop, is now
`ol_pm_pn16_nopad_pvt_deg` >= 30 deg at every corner. `passive_skew_corners`
(ss_pff, ff_pss) could not then be added alongside the 27-point grid (engine
gap, reported to chip-flow), so the skew numbers above come from a scratch
run, not a gate. That run's 331.75 MHz at `tt_pff` was mislabelled here as
fmax at `vctrl` = vdd: it is `vco_fmax_tt_mhz`, the frequency at
`vctrl` = vdd - 0.3 V. At `vctrl` = vdd, `tt_pff` reads 346.0 MHz, and
ff_m40c_vm10 (fast, cold, LOW supply) 362.9 MHz, both above the 333 MHz the
bound held at ff_m40c_vp10 alone (264.8 MHz there).

### Revision 2026-10-28 (after H1, the open items)

The owner approved H1-ada9eb (`log/H1-signoff.md`) and the open items it
carried are closed in the spec:

1. The skew corners `ss_pff` and `ff_pss` are swept (`corners.extra`), so
   every "all" measure is scored at them by the gates.
2. `vco_fmax_ff_mhz` is scored at every corner, against 370 MHz: the VCO
   peaks at fast, cold, low supply, not high, and the digital timing is
   rerun with the VCO clock at 2.7 ns to match. Its floor is 150 MHz, the
   highest lock point every corner keeps: at `ff_pss` the VCO tops out near
   158 MHz, so the 160 and 200 MHz lock points are lost there and the PLL
   works over a narrower range (the proposal's lock table leaves a lock
   point inside every corner's band); 200 MHz at typical stays
   `vco_fmax_tt_mhz`'s.
3. The mislabelled frequency above is corrected.
4. Period jitter is no longer a spec measure (see "Period jitter").

### Period jitter (estimate, not gated)

ngspice has no device transient noise, and the ring nodes are internal to
the subckt, so no bench here can print a period jitter. A separate estimate
with `trnoise` sources sized from the device models and added to the ring
read about 1.2 ps rms at typical and 2.5 ps rms at slow and hot, over 30-36
periods (+-30 %), leaving out flicker noise and loop-filter noise. The old
warning bound was 10 ps rms; the digital timing waiver needs far less than
the margin that leaves. The measure was a warning that never printed at any
run, so it is replaced by this note rather than kept as a permanent miss.

### Layout (checked by later gates, not by ngspice)

Generator-based; Magic and KLayout DRC clean; netgen LVS clean against the
sized netlist; C1 area reported. The footprint must leave room for the
digital side on the two-tile analog footprint, and the `vco_out` pin sits
on the edge nearest the prescaler.

### Out of scope here

Full-loop lock (< 50 us), reference spur and divided frequency against the
reference are msde cosim measures at the top level.
