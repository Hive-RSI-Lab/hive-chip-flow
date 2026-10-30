# A Charge-Pump PLL on Tiny Tapeout GF180

ECE 298A Term Project Proposal

Team: two ECE298A students (named in the source repository)

2026-09-24

## Abstract

We propose a classic type-II charge-pump phase-locked loop (PLL) for the Tiny Tapeout GF180 shuttle. A
phase-frequency detector (PFD), a ÷8 toggle-flop prescaler, a programmable feedback divider and a lock
detector are built from standard cells; a charge pump, a passive loop filter and a current-starved ring
VCO are analog. The loop multiplies a 5 to 12.5 MHz reference by P·N = 8 to 40 (P = 8, N = 1 to 5) onto
a VCO range of about 50 to 200 MHz (est.), with a nominal point of 6.25 MHz × 16 = 100 MHz and a lock
time under 50 µs (est.). Only the prescaler runs above 50 MHz. Digital blocks and the full loop are
verified in CocoTB against a real-number model; the analog blocks are characterised in ngspice across
corners. An alternative implementation moves the pump output to a tri-state pad and the filter and VCO
off chip.

## 1. Introduction and motivation

The charge-pump PLL is the standard clock multiplier in integrated circuits, and it is the one
circuit in which a digital phase detector, an analog integrator and an oscillator form a single
feedback loop. Building one exercises the whole mixed-signal flow: RTL and CocoTB for the digital
half, transistor-level design and ngspice for the analog half, and layout checked with DRC and LVS.
The loop multiplies the tile reference by a programmable ratio, so the result is directly observable on
the tile's digital outputs.

The design targets a two-tile-high analog tile built on `ttgf-analog-template` [5], with two analog
pins (`ua[0]`, `ua[1]`). Section 3 describes the design in its analog-tile form, and section 3.3 gives
an alternative implementation on the standard digital tile.

## 2. Target specifications

These are hand estimates; replacing them with ngspice results is the Nov 5 deliverable.

**Design constraint.** The maximum clock frequency for synthesized logic on the chip is estimated at
50 MHz. Only the ripple prescaler runs faster; the programmable divider, the PFD, the lock detector and
the ÷16 observation flop all run at 50 MHz or below (at most 42 MHz at the fast corner).

| Parameter | Target | Basis |
|---|---|---|
| Reference | `clk`, 5 to 12.5 MHz, nominal 6.25 MHz | well inside the 50 MHz tile clock limit |
| Prescaler | fixed ÷8 (P = 8), three toggle flops | the VCO reaches about 333 MHz at the fast corner (est.); ÷4 would leave 83 MHz, ÷8 gives 41.6 MHz |
| N | 1 to 5 (3-bit field; codes 0, 6 and 7 are not supported) | lock needs 50 MHz ≤ 8·N·f_ref ≤ 200 MHz, so N = 200/(8·5) = 5 at the slowest reference and N = 1 from 6.25 MHz up |
| Total ratio | P·N = 8, 16, 24, 32, 40 | output steps of 8·f_ref: 40 MHz at 5 MHz, 100 MHz at 12.5 MHz |
| VCO range | about 50 to 200 MHz typical, up to about 333 MHz at the fast corner (est.) | current-starved ring, about 100 MHz/V gain (est.); ring-inverter delay is 1.66× shorter at ff_n40C_3v60 than at tt_025C_3v30 (Liberty) |
| Nominal point | 6.25 MHz × 8 × 2 = 100 MHz | |
| Loop bandwidth | fn = ωn/2π ≈ 400 kHz at P·N = 16; open-loop crossover ≈ 490 kHz (f_ref/13) (est.) | the usual guideline is crossover below f_ref/10. At N = 1 (P·N = 8) and f_ref = 6.25 MHz crossover rises to ≈ 850 kHz ≈ f_ref/7.4, which breaks it (N = 1 meets it above 8.5 MHz); the Gardner sampling limit [1] still holds with 2× margin |
| Loop filter | Icp ≈ 20 µA, C1 ≈ 20 pF, R ≈ 22 kΩ, C2 ≈ C1/10 (est.) | ωn = √(Icp·Kvco/(2π·P·N·C1)) ≈ 2π·400 kHz with Kvco in rad/s/V (2π·100 MHz/V), ζ = R·C1·ωn/2 ≈ 0.55, at P·N = 16. The zero is at 360 kHz and C2 adds a pole at 4 MHz, so the phase margin is ≈ 47° (est.) |
| Lock time | under 50 µs (est.) | small-signal settling ≈ 4/(ζωn) ≈ 3 µs at P·N = 16 and ≈ 7 µs at P·N = 40, plus frequency acquisition (Vctrl slews at most Icp/(C1+C2) ≈ 0.9 V/µs, so a 1.5 V swing takes a few µs) |
| Jitter | simulated period jitter of the VCO, reported per corner; silicon: scope upper bound on the divided output | absolute jitter cannot be measured cleanly through the tile pads |
| Observed output | prescaler output (VCO ÷ 8) or one more toggle (÷ 16) on `uo_out[0]`: 3.1 to 25 MHz typical, at most 42 MHz at the fast corner | pads limit an output to roughly tens of MHz |

### 2.1 Prescaler and frequency plan

**Ratio.** The smallest power of two that keeps the prescaler output at or below 50 MHz at the fast
corner is 8: 333/2 = 166 MHz, 333/4 = 83 MHz and 333/8 = 41.6 MHz. ÷4 would have been enough at the
typical 200 MHz maximum (exactly 50 MHz) but not at the fast corner, so the design uses ÷8.

**Toggle-flop speed.** Each stage is a `gf180mcu_fd_sc_mcu7t5v0__dffq_1` with an `inv_1` from Q back to D.
With the Liberty tables (clock slew 0.15 ns, about 13 fF on Q), the loop is clk-to-Q + inverter + setup:

| Corner | clk→Q | inverter | setup | loop | Liberty min_period |
|---|---|---|---|---|---|
| tt_025C_3v30 | 1.18 ns | 0.13 ns | 0.39 ns | 1.70 ns (590 MHz) | 1.71 ns |
| ff_n40C_3v60 | 0.68 ns | 0.08 ns | 0.18 ns | 0.94 ns (1.06 GHz) | 0.93 ns |

The first stage sees at most 333 MHz (3.0 ns period, 1.5 ns half-period) at the fast corner, 3.2× the
0.94 ns loop and 3.2× the 0.47 ns minimum pulse width; even typical-corner cells clocked at 333 MHz keep
1.8× margin. The stage outputs are at most 166, 83 and 41.6 MHz.

**Lock table.** The loop locks where 50 MHz ≤ 8·N·f_ref ≤ 200 MHz (typical VCO range):

| f_ref | N that lock | f_vco | step 8·f_ref |
|---|---|---|---|
| 5 MHz | 2, 3, 4, 5 | 80, 120, 160, 200 MHz | 40 MHz |
| 6.25 MHz | 1, 2, 3, 4 | 50, 100, 150, 200 MHz | 50 MHz |
| 10 MHz | 1, 2 | 80, 160 MHz | 80 MHz |
| 12.5 MHz | 1, 2 | 100, 200 MHz | 100 MHz |

N = 1 needs f_ref ≥ 6.25 MHz, N = 3 needs f_ref ≤ 8.33 MHz, N = 4 needs f_ref ≤ 6.25 MHz, and N = 5 locks
only at 5 MHz. Because f_vco = 8·N·f_ref is continuous in f_ref, N = 1 to 5 over 5 to 12.5 MHz covers
40 to 500 MHz without gaps (40 to 100, 80 to 200, 120 to 300, 160 to 400, 200 to 500 MHz), so any VCO
band a corner produces inside that span still has a lock point.

**Filter area.** A 20 pF capacitor is large. At an assumed MIM density of 1 to 2 fF/µm² it needs
roughly 10,000 to 20,000 µm², a large share of a two-tile area; this is also why the alternative
implementation places the filter off chip.

**Reference spur.** Pump mismatch sets the reference spur. With a PFD reset pulse of about 1 ns (est.),
a 10 % UP/DN mismatch gives a static phase offset of about 0.1 ns (0.23° at 6.25 MHz) and a ripple on
Vctrl every reference cycle. The C2 pole at 4 MHz attenuates a 6.25 MHz spur by only about 5 dB, so
C2 = C1/10 does little for spurs. The spur level is reported from ngspice; it cannot be measured
through the pads.

## 3. Architecture

### 3.1 Block diagram

```
            +---------+  UP   +--------------+  Icp   +----------------+ Vctrl +-----------+
 clk(ref)-->|  PFD    |------>| charge pump  |------->| loop filter    |------>| VCO       |--> vco
            | (3-state|  DN   | (switched    |        | R + C1 || C2   |   |   | current-  |
       +--->|  flops) |------>|  currents)   |        +----------------+   |   | starved   |
       |    +---------+       +--------------+                             |   | ring      |
       |                                                   ua[0] (Vctrl) <-+   +-----------+
       |                                                                              |
       +---- [÷N divider] <---- [÷8 prescaler, toggle flops] <----------------------- vco
                   |                         |
   [lock detector] <--- UP/DN   uo_out[1] <--+          +--> [÷2 toggle] --> ÷8/÷16 --> uo_out[0]
   digital: PFD, prescaler, divider, lock detector, ÷16 flop; analog: pump, filter, VCO
   above 50 MHz: prescaler only
```

### 3.2 Blocks

**PFD (digital).** Two D flops clocked by the reference and the feedback, with an AND reset. A short
reset delay removes the dead zone. It is built from standard cells, with keep attributes on the delay
chain so synthesis does not remove it. Its width is checked in ngspice, not in RTL. Its feedback flop
and reset path see only the divided feedback, at most 41.6 MHz (N = 1, fast corner, during acquisition).

**Charge pump (analog).** Switched PMOS and NMOS current sources, with Icp set by a mirror from a bias.
The design concerns are UP/DN current mismatch across Vctrl, which causes a static phase offset and
reference spurs, and charge sharing at switching. A unity-gain buffer to equalise the switch nodes is
optional and goes in only if ngspice shows it is needed.

**Loop filter (analog).** A series R and C1 with shunt C2 on the Vctrl node. The node is brought out on
`ua[0]` for observation. The pad path (up to 5 pF [2]) sits in parallel with C2, so the on-chip C2 is
sized with the pad included: 2 pF plus 5 pF of pad drops the phase margin from about 47° to 32° at
P·N = 16 and from 33° to 23° at P·N = 40 (est.). A scope probe (about 15 pF) on `ua[0]` would push it
below 20°, so Vctrl is observed only through a high-impedance buffer or with the loop open.
Capacitance added at `ua[0]` adds to C2, not C1, so it cannot make up for a C1 that is too small; that
needs the R to C1 node brought out on a third analog pin.

**VCO (analog).** A current-starved inverter ring with an odd number of stages. Vctrl sets the starving
current. It is designed for monotonic tuning across corners, and the stage count is chosen from the
ngspice sweep.

**Prescaler (digital).** Three ripple toggle flops (`dffq_1` with an inverter from Q to D) divide the
VCO by 8. It is the only logic above 50 MHz, and it is placed next to the VCO output.

**Divider and observation (digital).** A programmable ÷N counter (N = 1 to 5; N = 1 passes the
prescaler output through) clocked by the prescaler output, at most 41.6 MHz. The observation output is
the prescaler output (VCO ÷ 8) or one more toggle flop (÷ 16), selected by `ui_in[7]`. At lock, VCO ÷ 8
is N·f_ref and the feedback output on `uo_out[1]` is f_ref, so the ratio of the two pins reads N.

**Lock detector (digital).** Lock asserts when UP and DN pulses have stayed shorter than a threshold for
K consecutive reference cycles, and drops when they do not. It is clocked by the reference.

### 3.3 Alternative implementation: digital tile, off-chip filter

The same loop can be built on the standard digital template. `uio[0]` acts as the charge pump: it is
driven high on UP, low on DN and tri-stated (`uio_oe` = 0) otherwise, the same arrangement as the
three-state phase comparator in a CD4046 [3]. During the PFD reset overlap, when UP and DN are both
high, the pin stays tri-stated. An off-chip series resistor into an off-chip RC loop filter turns the
pad into an approximate current source, with I ≈ (VDD − Vctrl)/R on UP and Vctrl/R on DN. The two match
only at Vctrl = VDD/2, and the current varies with Vctrl, so loop gain varies across the tuning range.
An off-chip VCO closes the loop through a `ui` input: a 4046-class part or a discrete oscillator in the
low tens of MHz. The on-chip prescaler, ÷N divider, PFD and lock detector are unchanged, so the loop
still multiplies by 8·N and the core stays at or below 50 MHz whatever the VCO. With a VCO of about
30 MHz, N = 5 (P·N = 40) needs a reference of about 0.75 MHz, so this implementation runs `clk` well
below the 5 to 12.5 MHz of section 2.

In this form the silicon holds the digital half of the loop, as an integer-N synthesizer IC with an
external VCO and loop filter does, but with a voltage-output pump, so it does not lock without the
off-chip parts. A middle form keeps an analog tile for the VCO alone (one analog pin for Vctrl) and puts
the pump on `uio[0]` and the filter off chip.

## 4. Interface and pin allocation

### Analog-tile implementation

| Pin | Dir | Use |
|---|---|---|
| `ui_in[2:0]` | in | N (1 to 5; codes 0, 6 and 7 not supported) |
| `ui_in[5:3]` | in | spare |
| `ui_in[6]` | in | PLL enable (power-down of pump and VCO) |
| `ui_in[7]` | in | observation select (÷ 8 or ÷ 16) |
| `uo_out[0]` | out | VCO ÷ 8 or ÷ 16 |
| `uo_out[1]` | out | feedback divider output |
| `uo_out[2]` | out | lock |
| `uo_out[3]`, `uo_out[4]` | out | UP, DN (debug; short pulses may not survive the pads) |
| `uo_out[7:5]` | out | driven low, spare |
| `uio[1:0]` | in | charge-pump current trim |
| `uio[7:2]` | in | spare |
| `ua[0]` | analog | Vctrl / loop filter node |
| `ua[1]` | analog | bias reference (external resistor), optional |
| `clk`, `rst_n`, `ena` | in | reference, standard |

### Alternative implementation

`ui_in[0]` external VCO in, `ui_in[3:1]` N, `ui_in[6:4]` spare, `ui_in[7]` enable; `uio[0]` pump out
(dynamic `uio_oe`); `uio[1]` observation select (input); `uo_out` shows the feedback divider, lock, UP,
DN and VCO ÷ 8 or ÷ 16.

## 5. Verification plan

**CocoTB, digital blocks.** The bench checks the PFD's UP/DN widths against known phase offsets, and
both frequency-detection directions. The dead-zone reset pulse has no width in zero-delay RTL; CocoTB
checks that it happens, and its width comes from ngspice. The bench checks the prescaler's ÷8 and the
÷16 tap, ÷N for every N from 1 to 5 (so the total ratio 8·N), and the lock detector's assert/deassert
thresholds.

**CocoTB, loop behaviour.** Under `ifdef SIM`, a real-number Verilog model stands in for the pump,
filter and VCO. It integrates Icp into the filter state each time step and sets the VCO period from
Vctrl, with Kvco and the range taken per corner from ngspice, up to the 333 MHz fast-corner maximum.
For the alternative implementation, Python models the off-chip RC and VCO from `uio_oe`/`uio_out` and
drives `ui_in[0]`. Lock is checked three ways, at every (f_ref, N) point of the lock table:

1. The lock flag rises within the bound.
2. Python edge timestamps show the average feedback period equal to the reference period, and the
   VCO period equal to the reference period divided by 8·N, with the phase error bounded.
3. The modelled Vctrl settles.

The loop must also relock after a change of N. The bench is mutation-tested with a swapped UP/DN, a
missing PFD reset, a divider off by one and a prescaler of ÷4.

**Timing.** Static timing constrains the prescaler output as a generated clock (VCO ÷ 8) at 50 MHz, and
everything after it (divider, PFD feedback flop, ÷16 flop) meets that. The reference domain (PFD
reference flop, lock detector) is constrained at 12.5 MHz. The three prescaler flops are checked
against the Liberty minimum period and pulse widths at the fast-corner VCO rate.

**ngspice, analog blocks.** PDK device models across corners (typical, fast, slow), supply ±10 % and
−40 to 125 °C. The analysis covers:

- charge pump: UP/DN matching against Vctrl, and charge sharing
- loop filter: AC response and phase margin
- VCO: frequency against Vctrl, Kvco, range and monotonicity, plus period jitter from transient noise,
  injected with `trnoise` sources [4] sized from the device noise models (an estimate)
- prescaler: the first toggle flop driven by the VCO at the fast-corner maximum
- full loop: a transient with the PFD, prescaler and divider built from the standard-cell SPICE
  netlists, showing Vctrl settling and lock

**Layout checks.** Layout is drawn in Magic or with a parameterised generator. Magic and KLayout DRC
run on every revision; because precheck runs DRC only, netgen LVS is part of this plan, and the
extracted parasitics are re-simulated before sign-off.

## 6. Schedule and milestones

| Date | Deliverable |
|---|---|
| Oct 8 | Digital RTL (PFD, prescaler, divider, lock detector) and the real-number loop model, CocoTB green in CI; first-pass pump/VCO schematics; implementation fixed (analog tile or alternative) |
| Nov 5 | Evaluation: ngspice corners for pump, filter, VCO and prescaler; full-loop transient lock; loop parameters fixed; layout under way |
| Nov 26 | System verification: layout DRC-clean, LVS clean, post-layout re-simulation, Actions and precheck green, submission ready |
| Dec 3 | Documentation and silicon measurement plan (Vctrl against N, lock range, divided-output jitter) |

## 7. Technical risks and mitigations

| Risk | Mitigation |
|---|---|
| Layout errors not caught by precheck (DRC only) | netgen LVS and extracted re-simulation |
| Hand layout overruns the schedule | small topology, generator where possible, layout started by early November |
| Filter capacitor too large for the area | bring out the R to C1 node for an off-chip C1 (a third analog pin); capacitance on `ua[0]` only adds to C2 |
| Pump mismatch gives spurs or offset | sized from the ngspice sweep, current trim on `uio[1:0]` |
| VCO range misses at corners | the reference is adjustable, and 8·N·f_ref covers 40 to 500 MHz without gaps |
| First prescaler flop too slow for the VCO | Liberty gives a 0.94 ns toggle loop at the fast corner against a 3.0 ns VCO period; confirmed in the ngspice prescaler transient |
| Loop dynamics vary with P·N: ζ drops from 0.78 at P·N = 8 to about 0.35 at P·N = 40 (ωn and ζ scale as √(Icp/(P·N))), and with C2 the phase margin falls from 55° to about 33° (est.) | Icp stays at 20 µA, since P·N = 8 to 40 lies inside the span the filter was sized for; doubling Icp with the trim on `uio[1:0]` at P·N = 32 to 40 lifts ζ to 0.55 to 0.49 (phase margin 47° to 43°); loop parameters fixed in the Nov 5 evaluation |
| Standard cells inside the analog tile | digital blocks hardened as a macro and placed by hand |
| Pads limit observation | divided outputs only |

## References

1. F. M. Gardner, "Charge-pump phase-lock loops," *IEEE Transactions on Communications*, vol. 28, no. 11, pp. 1849-1859, Nov. 1980.
2. Tiny Tapeout, "Analog specifications," https://tinytapeout.com/specs/analog/
3. Texas Instruments, *CD4046B CMOS Micropower Phase-Locked Loop* datasheet.
4. ngspice User's Manual (independent sources, `trnoise`), https://ngspice.sourceforge.io/docs.html
5. Tiny Tapeout, `ttgf-analog-template`, https://github.com/TinyTapeout/ttgf-analog-template
