# PLL digital side - brief for the nested /vde run

Top module: `tt_um_ihsan_sa_pll` (Tiny Tapeout GF180 analog-tile top, library
`gf180mcu_fd_sc_mcu7t5v0` at 3.3 V). The source is the reviewed proposal at
`../../brief/proposal-classic-pll.md`; this brief names what the digital side
owns and exactly what crosses to the analog hard macro `pll_analog`. The
crossing signals are fixed in `../../interface.yaml` - do not rename, widen
or add one; a change there goes back through the msde `split` step.

## What this side does

The digital side is the whole tile except the charge pump, loop filter,
VCO and bias, which are one analog hard macro. It contains:

1. **Phase-frequency detector (PFD).** Two D flops, one clocked by the
   reference `clk`, one by the feedback divider output, with an AND reset
   and a short kept delay chain (keep attributes) to remove the dead zone.
   Outputs are the active-high pulses `pfd_up` and `pfd_dn`. `pfd_up` high
   means the reference leads and the pump must source current into the
   filter (raise Vctrl, speed up the VCO); `pfd_dn` high means the feedback
   leads and the pump must sink. The reset pulse has no width in zero-delay
   RTL; the bench checks it happens, ngspice (analog side / cosim) measures
   its width.
2. **/8 prescaler.** Three ripple toggle flops (`dffq_1` with an `inv_1`
   from Q to D) clocked by `vco_out`. The only logic above 50 MHz: the first
   stage sees up to 333 MHz at the fast corner (VCO typical 50-200 MHz).
   Its output `clk_pre` (VCO/8, at most 42 MHz) clocks everything after it.
3. **Programmable /N divider.** N = 1 to 5 from `ui_in[2:0]` (codes 0, 6, 7
   unsupported: treat as N = 1 or hold the last legal value, state which).
   N = 1 passes `clk_pre` through. Output `clk_fb` goes to the PFD feedback
   flop and to `uo_out[1]`.
4. **Lock detector.** Clocked by `clk`. Lock asserts when both `pfd_up` and
   `pfd_dn` have stayed shorter than a threshold for K consecutive reference
   cycles and drops when they do not. Output on `uo_out[2]`.
5. **Observation.** `uo_out[0]` = `clk_pre` (VCO/8) or one more toggle flop
   (VCO/16), selected by `ui_in[7]`. `uo_out[3]`/`uo_out[4]` = `pfd_up`/
   `pfd_dn` for debug.
6. **Control registers.** `pll_en` (from `ui_in[6]`), `cp_trim0`/`cp_trim1`
   (from `uio[1:0]`, inputs, `uio_oe` = 0) are registered on `clk` and
   driven to the analog macro; they are quasi-static.

Clock domains: `clk_ref` (tile `clk`, 5-12.5 MHz, constrained at 12.5 MHz),
`clk_vco` (`vco_out`, prescaler flops only, checked against Liberty min period
and pulse width at 333 MHz), `clk_pre` (generated clock VCO/8, constrained at
50 MHz), `clk_fb` (`clk_pre`/N, <= 42 MHz). Reset: `rst_n` asynchronous,
active low, resets everything; the prescaler and divider must come out of
reset cleanly with `vco_out` already running.

## Crossing signals (top-level ports of the digital core, TT pin for standalone harden)

Every crossing signal is a top-level port of the digital core module carrying
exactly this name, and the `tt_um_ihsan_sa_pll` wrapper maps it to the spare
TT pin listed so the digital side hardens and releases alone. At
`top_harden` the msde integrator reconnects these nets to the `pll_analog`
macro pins of the same name.

| Signal | Dir | Level | Domain | Width | Standalone TT pin | Meaning |
|---|---|---|---|---|---|---|
| `vco_out` | a2d (input) | cmos_3v3 | clk_free | 1 | `ui_in[3]` | VCO output, full-swing clock, up to 333 MHz; clocks the prescaler only |
| `pfd_up` | d2a (output) | cmos_3v3 | clk_ref | 1 | `uo_out[3]` | PFD UP pulse, active high, pump sources Icp |
| `pfd_dn` | d2a (output) | cmos_3v3 | clk_fb | 1 | `uo_out[4]` | PFD DN pulse, active high, pump sinks Icp |
| `pll_en` | d2a (output) | cmos_3v3 | clk_ref | 1 | `uo_out[5]` | registered `ui_in[6]`; 1 enables pump, bias and VCO, 0 powers them down |
| `cp_trim0` | d2a (output) | cmos_3v3 | clk_ref | 1 | `uo_out[6]` | registered `uio[0]`, charge pump current trim bit 0 |
| `cp_trim1` | d2a (output) | cmos_3v3 | clk_ref | 1 | `uo_out[7]` | registered `uio[1]`, charge pump current trim bit 1 |

The analog pads `ua[0]` (Vctrl) and `ua[1]` (bias reference) belong to the
analog macro alone (`interface.yaml` `ua_pins`); the digital side has no port,
wire or logic named `vctrl` or `bias_ref` and never touches `ua[*]`.

`pfd_up`/`pfd_dn` on `uo_out[3]`/`uo_out[4]` doubles as the proposal's debug
observation, so those pins stay in the final tile as well. `uo_out[5:7]`
carry `pll_en`/`cp_trim` only in the standalone harden; the integrator may
drive them low in the final tile.

## Pin map (final tile, from the proposal)

`ui_in[2:0]` N; `ui_in[3]` `vco_out` in standalone harden, spare otherwise;
`ui_in[5:4]` spare; `ui_in[6]` PLL enable; `ui_in[7]` observation select
(0 = VCO/8, 1 = VCO/16). `uo_out[0]` VCO/8 or /16; `uo_out[1]` `clk_fb`;
`uo_out[2]` lock; `uo_out[3]` UP; `uo_out[4]` DN; `uo_out[7:5]` see above.
`uio[1:0]` trim inputs; `uio[7:2]` spare inputs; all `uio_oe` = 0,
`uio_out` = 0. `ua[0]`/`ua[1]` are the analog macro's pads, not this side's. `clk` reference,
`rst_n`, `ena` standard.

## Requirements and measures on this side

- /8 exactly on `vco_out`; the /16 tap exactly; /N exact for every N in 1-5,
  so `clk_fb` = `vco_out` / (8*N).
- PFD: UP/DN pulse widths track known phase offsets in both directions;
  frequency detection in both directions (reference faster gives net UP,
  slower gives net DN); the reset pulse occurs whenever both are high;
  UP and DN never both stay high beyond the reset delay.
- Lock detector: assert/deassert thresholds as specified (choose K and the
  pulse threshold, state them in spec.yaml); no assertion while UP or DN
  pulses are wide.
- Loop behaviour under `ifdef SIM`: a real-number Verilog model of pump,
  filter and VCO (Icp 20 uA, C1 20 pF, R 22 kOhm, C2 2 pF, Kvco about
  100 MHz/V, range 50-200 MHz typical, up to 333 MHz fast) closes the loop in
  CocoTB. At every (f_ref, N) point of the proposal's lock table: lock rises
  within 50 us, mean feedback period equals the reference period, VCO
  period equals reference period / (8*N), modelled Vctrl settles; the loop
  relocks after a change of N. This model is bench-only; nothing of it is
  synthesised.
- Mutation set the bench must kill: swapped UP/DN, missing PFD reset,
  divider off by one, prescaler /4.
- Timing: `clk_pre` generated clock at 50 MHz met by divider, PFD feedback
  flop and /16 flop; reference domain at 12.5 MHz; prescaler flops checked
  against Liberty min period (0.93 ns ff, 1.71 ns tt) and min pulse width at
  a 3.0 ns `vco_out` period. Keep attributes on the PFD delay chain survive
  synthesis (check in the synthesised netlist).
- Hardens standalone as `tt_um_ihsan_sa_pll` on the TT GF180 analog-tile
  footprint with the pins above; the analog macro's area is reserved by the
  msde integrator later, not by this run.

The joined-loop measures (divided frequency against reference, lock with the
real pump/VCO, PFD reset width, phase margin) are top-level cosim measures
owned by msde, not by this run.
