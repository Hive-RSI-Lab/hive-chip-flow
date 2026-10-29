# PLL analog side - brief for the nested /ade run

Cell: `pll_analog`, a hard macro placed inside the Tiny Tapeout GF180
analog-tile top `tt_um_ihsan_sa_pll`. GF180MCU, 3.3 V supply, digital
interface levels compatible with `gf180mcu_fd_sc_mcu7t5v0` cells. The source
is the reviewed proposal at `../../brief/proposal-classic-pll.md`; this brief
names what the analog side owns, exactly what crosses to the digital side,
and which pins go to the tile's analog pads. Both lists are fixed in
`../../interface.yaml` (`signals` and `ua_pins`) - do not rename, widen or
add one; a change there goes back through the msde `split` step.

## What this side does

The analog cell is the charge pump, the passive loop filter, the
current-starved ring VCO and their bias. The PFD, the /8 prescaler, the /N
divider, the lock detector and all control decoding are digital and are NOT
in this cell; the cell sees only the pins below.

1. **Charge pump.** Switched PMOS (UP) and NMOS (DN) current sources,
   Icp nominal 20 uA, set by a mirror from the bias. `pfd_up` high sources
   Icp into the filter node (`vctrl` rises); `pfd_dn` high sinks Icp from it.
   Both low: no current, node holds. Both high (the PFD reset overlap, about
   1 ns): net current as close to zero as matching allows. `cp_trim1:0`
   select the mirror ratio: code 00 = 20 uA nominal, and the codes must
   reach 2x (40 uA) - the proposal's fix for the low damping at P*N = 32-40;
   choose the other two steps and state them. Concerns: UP/DN mismatch
   against `vctrl` over the tuning range (static phase offset, reference
   spur), charge sharing at switching. A unity-gain buffer equalising the
   switch nodes goes in only if ngspice shows it is needed.
2. **Loop filter.** Series R and C1 with shunt C2 on `vctrl`: R about
   22 kOhm, C1 about 20 pF, C2 about 2 pF (est.), sized for fn about 400 kHz
   and zeta about 0.55 at P*N = 16 with Kvco about 2*pi*100 MHz/V. `vctrl` is
   routed to the tile pad `ua[0]` (`ua_pins`): up to 5 pF of pad sits in parallel with C2, so
   size C2 with the pad included (phase margin estimate 47 deg -> 32 deg
   with the pad at P*N = 16). The 20 pF C1 is the area risk; if it does not
   fit, say so in OPEN rather than shrinking it silently (capacitance added
   at `ua[0]` adds to C2, not C1).
3. **VCO.** Current-starved inverter ring, odd stage count chosen from the
   ngspice sweep, `vctrl` sets the starving current. Positive Kvco: higher
   `vctrl` gives higher frequency (the digital PFD polarity assumes this).
   Range about 50-200 MHz typical, up to about 333 MHz at ff_n40C_3v60,
   monotonic across corners, Kvco about 100 MHz/V (est.). The output is
   buffered to a full-swing 3.3 V CMOS clock `vco_out` capable of driving one
   `dffq_1` clock pin plus one buffer (about 15 fF) with edges fast enough at
   333 MHz (slew <= 0.15 ns target, the Liberty table condition the
   prescaler was checked at).
4. **Bias.** A reference current for the pump mirror and VCO. `bias_ref` is
   routed to the tile pad `ua[1]` (`ua_pins`) for an optional external resistor to ground; the cell
   must also work with `ua[1]` floating (an internal default bias), and must
   not be damaged by a probe or ESD-class capacitance on it.
5. **Power-down.** `pll_en` = 0 turns off the pump, bias and VCO (`vco_out`
   held at a static logic level, no oscillation, standby current reported);
   `pll_en` = 1 enables them. Quasi-static input.

## Pins of `.subckt pll_analog`

Exactly the six crossing signals, the two analog pad pins (`interface.yaml`
`ua_pins`), one supply and one ground - ten pins, no other. `top_harden`
joins the two sides by the signal names and routes each `ua_pins` entry to
its pad; the digital side never sees `vctrl` or `bias_ref`.

| Pin | Dir | Level | Domain | Width | Meaning |
|---|---|---|---|---|---|
| `vco_out` | a2d (output) | cmos_3v3 | clk_free | 1 | buffered VCO clock to the digital /8 prescaler, up to 333 MHz |
| `pfd_up` | d2a (input) | cmos_3v3 | clk_ref | 1 | UP pulse, active high, source Icp into `vctrl` |
| `pfd_dn` | d2a (input) | cmos_3v3 | clk_fb | 1 | DN pulse, active high, sink Icp from `vctrl` |
| `pll_en` | d2a (input) | cmos_3v3 | clk_ref | 1 | 1 = run, 0 = pump, bias and VCO off |
| `cp_trim0` | d2a (input) | cmos_3v3 | clk_ref | 1 | pump current trim bit 0 |
| `cp_trim1` | d2a (input) | cmos_3v3 | clk_ref | 1 | pump current trim bit 1 |
| `vctrl` | analog pad | analog | - | 1 | loop filter node, to tile pad `ua[0]` (`ua_pins: vctrl: 0`, up to 5 pF pad) |
| `bias_ref` | analog pad | analog | - | 1 | optional external bias resistor node, from tile pad `ua[1]` (`ua_pins: bias_ref: 1`) |
| `vdd` | supply | 3.3 V | - | 1 | the one supply pin |
| `vss` | ground | 0 V | - | 1 | the one ground pin |

Digital inputs (`pfd_up`, `pfd_dn`, `pll_en`, `cp_trim*`) are driven by
standard-cell outputs and must present a CMOS-gate load (no DC current);
inversion for the PMOS UP switch is done inside this cell. `vco_out` is the
only analog-to-digital logic output. `vctrl` and `bias_ref` go straight from
the macro to the pads; they are not digital ports.

## Split devices

None. No standard cell sits inside the analog macro; the prescaler's first
toggle flop is on the digital side, so `vco_out` is a real clock crossing.

## Requirements and measures on this side (ngspice, PDK corners typ/fast/slow, supply +-10 %, -40 to 125 C)

- Charge pump: Icp per trim code; UP/DN current matching against `vctrl`
  over 0.3-3.0 V (report mismatch %); charge sharing / net charge per
  switching event with both inputs high for 1 ns; leakage on `vctrl` with
  both inputs low.
- Loop filter: AC response of the pump-filter-VCO open loop at P*N = 8, 16
  and 40 with the digital divider as an ideal 1/(8N); crossover and phase
  margin with and without 5 pF on `vctrl`. Phase margin target >= 45 deg at
  P*N = 16 without pad, and the numbers with pad reported.
- VCO: frequency against `vctrl`, Kvco, range, monotonicity per corner;
  period jitter from transient noise (`trnoise` sized from device models);
  `vco_out` swing, rise/fall and duty at 333 MHz into the stated load;
  frequency at `vctrl` = vdd (fast corner) to confirm the 333 MHz bound the
  prescaler was checked against, or report the true maximum.
- Prescaler drive check: `vco_out` driving the extracted SPICE netlist of one
  `gf180mcu_fd_sc_mcu7t5v0__dffq_1` + `inv_1` toggle stage at the fast-corner
  maximum, toggling cleanly.
- Power-down: standby current with `pll_en` = 0; start-up time to a running
  `vco_out` after `pll_en` rises.
- Layout: generator-based where possible; Magic and KLayout DRC clean,
  netgen LVS clean against the sized netlist, post-extraction re-simulation
  of Kvco and Icp; C1 area reported. The macro footprint must leave room for
  the digital side on the two-tile analog footprint, and the `vco_out` pin
  should sit on the edge nearest where the prescaler will be placed.

The joined-loop measures (full-loop transient lock with the real digital
PFD/prescaler/divider, lock time < 50 us, reference spur, divided frequency
against reference) are top-level cosim measures owned by msde, not by this
run.
