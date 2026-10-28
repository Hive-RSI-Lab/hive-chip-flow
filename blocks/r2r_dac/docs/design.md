# R-2R DAC design document

An 8-bit R-2R DAC on one Tiny Tapeout analog tile in GF180MCU at 3.3 V, for ECE 298A at the University of Waterloo. This document describes the design as released (round 3, signed off in [`signoffs.md`](signoffs.md)), which main's `blocks/r2r_dac` has carried since [#7](https://github.com/ihsan-sa/ece298a-r2r-dac/pull/7). Every number here comes from a recorded gate report there:

- DC, step and glitch results over the corners: `blocks/r2r_dac/analog/reports/recorded/gate-sim_pvt.json` (recorded 2026-10-13T06:12)
- post-layout results: `blocks/r2r_dac/analog/reports/recorded/gate-pex_sim.json`
- mixed-signal co-simulation: `blocks/r2r_dac/reports/cosim_measures.json`
- tile hardening, DRC, LVS and precheck: `blocks/r2r_dac/reports/recorded/gate-*.json`
- the flow's own narrative of all three rounds: `blocks/r2r_dac/reports/shakedown.md`

## What it does

The digital front end drives an 8-bit code, `dac_code`, into an analog macro. The macro switches each bit onto an R-2R ladder through a standard-cell buffer, and the ladder's MSB node is the output, `vout`, on analog pad `ua[0]`. The output spans 0 to 255/256 × VDD, which is 3.287 V at 3.3 V, in steps of 1 LSB = 12.9 mV.

| pin | use |
|---|---|
| `ui_in[7:0]` | code input |
| `uio_in[1:0]` | `mode`: 00 load, 01 ramp, 10 sine, 11 hold |
| `uio_in[2]` | `slow`: 0 steps the ramp and sine counter every clock, 1 every 256 clocks |
| `uo_out[7:0]` | echo of `dac_code` |
| `ua[0]` | `vout`, the analog output |
| `clk`, `rst_n` | 50 MHz clock (20 ns), active-low reset |

The modes (`blocks/r2r_dac/digital/rtl/r2r_dac_digital.v`):

- **00, load.** `dac_code` takes `ui_in` at each rising edge, and the code register keeps a copy.
- **01, ramp.** `dac_code` is a free-running 8-bit counter that wraps from 255 to 0.
- **10, sine.** `dac_code` is a 256-entry table, round(127.5 + 127.5 × sin(2πi/256)), indexed by the same counter.
- **11, hold.** `dac_code` holds the last code captured in mode 00, whatever ramp or sine did in between.

`dac_code` is registered, so it changes only on a clock edge, and a mode change never shows a code that belongs to neither the old source nor the new one. `rst_n` is registered once before use, so reset takes effect one edge later. These are the nine requirements in `blocks/r2r_dac/top/spec/spec.yaml` (REQ-RESET through REQ-ECHO).

## How the ladder and drivers were sized

The ladder (`blocks/r2r_dac/analog/netlist/r2r_dac_analog.cir`) is a standard R-2R with the output at the MSB rung and a 2R termination to `vss` at the LSB end. Each bit drives its 2R branch through one `gf180mcu_fd_sc_mcu7t5v0__buf_20`, the largest buffer in the library. Every resistor is one `ppolyf_u` unit of the same width, so the ladder has 25 of them: 16 in the eight driven 2R branches, 7 in the series rungs, and 2 in the termination.

| parameter | value |
|---|---|
| unit width `ru_w` | 2.0 µm |
| unit length `ru_l` | 150 µm |
| unit R | 27.0 kΩ (`ppolyf_u`, 350 Ω/□) |
| trim unit length `rb_l` | 148.36 µm |
| driver | `buf_20`, Ron at tt 27 °C 1 mA: 163 Ω pull-down, 444 Ω pull-up |
| assumed pad load | 100 Ω + 7 pF |

**Why 27 kΩ.** The brief asked for a ladder that settles in one 50 MHz period into the pad. The driver's Ron depends on the bit's state (pull-up and pull-down differ by about 280 Ω), and that difference can't be trimmed out, so INL scales roughly as ΔRon/R. Swept at tt, the worst INL was 41 LSB at R = 250 Ω (and not monotonic), 3.5 LSB at 1.86 kΩ, 0.35 LSB at 18 kΩ, 0.23 LSB at 27 kΩ and 0.17 LSB at 36 kΩ. At 250 Ω the ladder settles in 19.3 ns; at 27 kΩ the time constant into the pad is about (27.1 kΩ + 100 Ω) × 7 pF ≈ 190 ns. No unit R meets both targets with `buf_20` at 3.3 V, so the owner kept 27 kΩ for accuracy and accepted about 1.2 µs settling. The settling bound became 1.5 µs (9 ln 2 τ) and the 10–90 % bound 0.5 µs (2.2 τ at ss).

**Trim.** In each driven branch, the second unit is shortened from 150 µm to 148.36 µm so that its body resistance is lower by the driver's average Ron at tt (about 295 Ω, the mean of pull-up and pull-down). Branch plus driver then totals 2R on average. The termination has no driver and no trim.

## Results over the corners

ngspice, pre-layout, at a fixed 3.3 V: the brief's nine corners (tt, ff, ss × −40, 25, 125 °C) plus two passive skews at 27 °C (`tt_pss`, `tt_pff`: typical transistors with slow or fast resistors). The sim_pvt gate passed with no errors and 17 warnings, all on two derived bounds the owner set as trade-offs: 10–90 % edges at ss, and INL with one unit off by 1 %.

### DC: DNL, INL, gain (bound ±0.5 LSB)

| corner | DNL max (LSB) | INL max (LSB) | monotonic | FS gain error (LSB) | INL, one unit ±1 % (LSB) |
|---|---|---|---|---|---|
| tt −40 °C | **0.141** | 0.210 | yes | +0.000 | 0.526 |
| tt 25 °C | 0.049 | 0.232 | yes | −0.005 | 0.526 |
| tt 125 °C | 0.084 | 0.310 | yes | −0.011 | 0.602 |
| ff −40 °C | 0.140 | 0.205 | yes | +0.000 | 0.522 |
| ff 25 °C | 0.049 | 0.228 | yes | −0.004 | 0.523 |
| ff 125 °C | 0.080 | 0.303 | yes | −0.011 | 0.595 |
| ss −40 °C | 0.117 | 0.218 | yes | −0.001 | 0.528 |
| ss 25 °C | 0.035 | 0.243 | yes | −0.006 | 0.530 |
| ss 125 °C | 0.134 | **0.355** | yes | **−0.014** | **0.649** |
| tt, slow R | 0.114 | 0.219 | yes | −0.001 | 0.530 |
| tt, fast R | 0.055 | 0.291 | yes | −0.010 | 0.579 |

The quiescent supply current is 73 to 110 µA.

**Mismatch.** The GF180 poly resistor models have no per-instance mismatch (`ppolyf_u` has `mis_r = 0`, and `res_statistical` is global only), so Monte Carlo can't move DNL or INL. The last column stands in for it: the worst INL when any one unit is off by ±1 %. It exceeds 0.5 LSB at every corner (warning, accepted).

### Step: full-scale settling (bound 1.5 µs) and edges (bound 0.5 µs)

| corner | settle to 0.5 LSB, rise / fall (µs) | 10–90 %, rise / fall (ns) | supply peak (mA) |
|---|---|---|---|
| tt −40 °C | 1.198 / 1.197 | 424 / 420 | 18.1 |
| tt 25 °C | 1.189 / 1.186 | 420 / 416 | 15.5 |
| tt 125 °C | 1.188 / 1.187 | 420 / 416 | 12.6 |
| ff −40 °C | 0.961 / 0.959 | 338 / 338 | **22.8** |
| ff 25 °C | 0.954 / 0.949 | 338 / 334 | 20.3 |
| ff 125 °C | 0.951 / 0.948 | 338 / 334 | 16.5 |
| ss −40 °C | **1.441 / 1.436** | **506 / 506** | 13.4 |
| ss 25 °C | 1.430 / 1.424 | 503 / 499 | 10.6 |
| ss 125 °C | 1.428 / 1.420 | 506 / 503 | 8.6 |
| tt, slow R | 1.426 / 1.423 | 503 / 499 | 15.4 |
| tt, fast R | 0.955 / 0.952 | 338 / 334 | 15.4 |

Settling is set by the resistor corner, not the transistors: slow resistors give about 1.43 µs and fast ones about 0.95 µs whatever the transistor corner. The supply peak stays under its 30 mA bound everywhere.

### Glitch at the mid-code transition (127 ↔ 128)

| corner | energy up / down (fV·s) | peak up / down (µV) | worst energy with ±200 ps bit skew (fV·s) |
|---|---|---|---|
| tt −40 °C | 0.8 / 1.7 | 19 / 29 | 8,610 |
| tt 25 °C | 1,620 / 2.3 | 60 / 32 | 13,979 |
| tt 125 °C | 11,653 / 987 | 406 / 37 | 21,649 |
| ff −40 °C | 0.5 / 1.1 | 12 / 25 | 20,732 |
| ff 25 °C | 2,024 / 1.6 | 74 / 28 | 23,669 |
| ff 125 °C | **14,294** / 2,350 | **497** / 86 | **31,807** |
| ss −40 °C | 0.9 / 2.2 | 19 / 32 | 6,679 |
| ss 25 °C | 3,445 / 2.5 | 126 / 36 | 12,492 |
| ss 125 °C | 12,706 / 628 | 442 / 37 | 20,023 |
| tt, slow R | 0.9 / 1.6 | 15 / 23 | 6,259 |
| tt, fast R | 12,375 / 782 | 431 / 47 | 29,765 |

The glitch is small because the 190 ns RC filters it. The flow noted that the pre-layout glitch window (60 ns) is short against that time constant, so these figures probably under-read; the post-layout bench below is the one to trust.

### Worst corner

**ss** is the worst corner for accuracy and speed: ss 125 °C has the worst INL (0.355 LSB), gain error (−0.014 LSB) and one-unit sensitivity (0.649 LSB), and ss −40 °C has the slowest settling (1.441 µs) and the only edges over 0.5 µs (506 ns). ff 125 °C has the largest glitch and ff −40 °C the largest supply peak. Every hard bound passes at every corner; the misses are the two warning-level trade-offs above.

### Post-layout

The pex_sim gate ran the step and glitch benches on the netlist extracted from the macro's layout (2,910 parasitic R, 2,084 C) and passed:

| measure | rise / up | fall / down | bound |
|---|---|---|---|
| settling to 0.5 LSB | 1.202 µs | 1.197 µs | 1.5 µs |
| 10–90 % | 424 ns | 422 ns | 500 ns |
| glitch energy | 23.8 pV·s | 14.9 pV·s | 260 pV·s |
| glitch peak | 21.8 mV | 22.5 mV | 52 mV |

### Co-simulation

The released RTL drove the real ladder (ideal drivers, nominal 27 kΩ, the 100 Ω + 7 pF pad) through all 256 codes with no code mismatches: DNL and INL 0.006 LSB, full scale 3.287 V, gain error −0.005 LSB, settling 1.19 µs both ways.

## How the tile was verified

| step | result |
|---|---|
| analog: spec lint, netlist lint, sim_tt, sim_pvt | pass |
| analog bench strength (does the bench catch a broken netlist?) | pass, 33 of 33 mutants killed |
| analog layout | 208 × 140 µm macro, 25 `ppolyf_u` + 8 `buf_20`, supplies strapped on Metal3 |
| analog DRC, LVS | pass |
| analog post-layout simulation (pex_sim) | pass, above |
| digital: lint, simulation, holdout tests, formal (depth 12), cover, mutation | pass |
| digital standalone timing at ss 125 °C | −0.87 ns at 20 ns (about 47 MHz met), waived by the owner |
| tile harden (LibreLane, 1×2 tile) | pass: harden's own DRC, LVS and antenna clean, setup slack +0.29 ns |
| tile DRC (magic, with the macro filled with dummy COMP) | pass |
| tile LVS (netgen) | pass: circuits match uniquely, macro 480 transistors + 25 resistors on both sides |
| Tiny Tapeout precheck | pass, TT's own klayout DRC clean |
| co-simulation | pass, above |
| release, all three blocks | pass, signed off by the owner (H2-1452e6, H2-4da295, H2-a7bfe1) |

The tile's signed-off GDS is `blocks/r2r_dac/top/harden/runs/run/final/gds/tt_um_r2r_dac_digital.gds`.

## Known limits

- **Settling.** The DAC settles in about 1.2 µs (1.44 µs at ss), not the brief's 20 ns. It updates at 50 MHz, but `vout` follows a code change about 60 clocks later, so the ramp and sine modes are usable with `slow = 1` (one step per 256 clocks, 5.12 µs) and smear with `slow = 0`. A buffered output or lower-Ron switches would be needed for 20 ns.
- **Mismatch** can't be simulated in this PDK; one unit off by 1 % costs up to 0.65 LSB of INL.
- **Corners** are at a fixed 3.3 V; supply variation wasn't swept in the final round. Round 1, on the engine's default corners, ran ss at 2.97 V and ff at 3.63 V and stayed inside 0.5 LSB for DNL and INL.
