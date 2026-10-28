# r2r_dac_digital - the digital front end of r2r_dac

The digital side of the `/msde` block `r2r_dac` (../../brief/spec.md): the
8-bit code source that drives the analog R-2R ladder macro. It is a plain
synchronous block in gf180mcu_fd_sc_mcu7t5v0 at 3.3 V, laid out and run
exactly like a standalone `/vde` block so it hardens, simulates and
releases on its own. cocotb is pinned at 2.0.1.

## What it does

Produces an 8-bit unsigned DAC code, `dac_code[7:0]`, every clock, from one
of three sources chosen by mode pins:

| mode[1:0] | name     | dac_code                                              |
|-----------|----------|-------------------------------------------------------|
| 00        | register | the code register, which captures `ui_in[7:0]` on every clk while in this mode |
| 01        | ramp     | a free-running 8-bit counter, +1 per step, wrapping 255 -> 0 |
| 10        | sine     | a 256-entry sine table indexed by the same free-running counter |
| 11        | hold     | the code register, frozen (ui_in ignored) - for DC/static-code measurement |

- `dac_code` is a REGISTERED output: it changes only on a rising edge of
  `clk`, never combinationally from `ui_in` or `mode`, so the analog side
  sees at most one code change per 20 ns period with all 8 bits switching
  in the same clock edge (the mid-code glitch measure at the top level
  depends on this).
- The free-running counter advances every clock when `slow` is 0, and
  every 256th clock when `slow` is 1 (so a ramp period is 256 x 20 ns =
  5.12 us fast, or 1.31 ms slow, visible on a scope). The counter runs
  in every mode; it is only the output mux that changes.
- Sine table: entry i (i = 0..255) is
  `round(127.5 + 127.5 * sin(2*pi*i/256))`, unsigned 8-bit, one full
  period per 256 steps, values 0..255, entry 0 = 128 (round half up),
  entry 64 = 255, entry 192 = 0. The table is a constant in the RTL (a
  case statement or an initialised ROM array), not loaded at runtime.
- The code register captures `ui_in[7:0]` on every clk while mode == 00
  and holds while mode == 11 (and while in ramp/sine, so switching
  register -> ramp -> hold keeps the last captured code).
- `rst_n` is a synchronous, active-low reset, registered once inside the
  block (one flop) before use, so it acts one clk later: on every clk edge
  after one that sampled `rst_n` low, the code register is 0, the counter
  is 0 and the slow prescaler is 0, so `dac_code` is 0 (ladder at zero
  scale). Release is delayed by the same one clk.
- `ena` (the Tiny Tapeout enable) is accepted and ignored, as TT designs
  normally do.
- `dac_code` is also echoed on `uo_out[7:0]` so the code the ladder sees is
  observable at the digital pins and the digital side can be tested and
  released without the macro.

## Interface (top-level ports)

| port          | dir | width | meaning |
|---------------|-----|-------|---------|
| clk           | in  | 1 | free-running system clock, 50 MHz (20 ns period), the `clk_sys` domain |
| rst_n         | in  | 1 | synchronous, active-low reset (registered once) |
| ena           | in  | 1 | TT enable, ignored |
| ui_in         | in  | 8 | code register input (captured while mode == 00) |
| mode          | in  | 2 | source select, see table; = TT `uio_in[1:0]` |
| slow          | in  | 1 | counter prescale: 0 = step every clk, 1 = every 256 clks; = TT `uio_in[2]` |
| dac_code      | out | 8 | the DAC code to the analog macro - the d2a crossing signal in ../../interface.yaml |
| uo_out        | out | 8 | = dac_code, for observation and standalone release |

`dac_code` is the crossing signal ../../interface.yaml names, direction
d2a, level cmos_3v3, domain clk_sys, width 8. It is a top-level port of
exactly that name on this block. For the standalone digital release it is
mapped to spare TT pins (`uio_out[7:0]`, with `uio_oe` driven high on
those bits) as well as being what the analog macro's `dac_code[7:0]` pins
connect to at `top_harden`; the ladder side sees only CMOS gate inputs
(one gf180mcu_fd_sc_mcu7t5v0 buffer input per bit), so no special drive
strength is required on this side.

`uio_in[7:3]` are unused inputs; `uio_oe`/`uio_out` bits not used above
are driven 0.

## Requirements the top-level brief puts on this side

- Registered `dac_code`, one change per clock edge, all bits from the same
  edge (glitch-energy measure at the top depends on it).
- Ramp mode must visit all 256 codes in order, wrapping, so the top-level
  cosim can sweep DNL/INL/monotonicity by running the ramp.
- Sine table must match the formula above exactly (the bench should
  compute the reference table in Python from the same formula and compare
  all 256 entries).
- cocotb 2.0.1 bench covering: reset value, register capture and hold,
  ramp sequence and wrap, slow prescale, sine table (all 256 entries), and
  mode switching without a glitch cycle (no cycle where `dac_code` is
  neither the old source's value nor the new one).
- Hardens in the standard /vde flow at 50 MHz on gf180mcu_fd_sc_mcu7t5v0,
  3.3 V, and passes the TT precheck on its own.

## Not on this side

- Anything resistive: the ladder, the 8 switch driver cells and the
  analog output are in the analog macro (../../analog/brief/spec.md).
- Code-to-voltage measures (DNL, INL, settling, glitch energy) are
  top-level cosim measures, not this side's.

## Requirements (ids used by spec.yaml, tb/ and formal/)

- REQ-RESET: on every clk edge after one that sampled rst_n low, dac_code (and uo_out) is 0, and the counter/prescaler restart from 0 (rst_n is registered once, so reset and release both act one clk late).
- REQ-CAPTURE: in mode 00, dac_code takes the value ui_in had at the previous rising edge.
- REQ-HOLD: in mode 11, dac_code equals the last code captured in mode 00 and ignores ui_in; ramp/sine do not disturb the captured code.
- REQ-RAMP: in mode 01 with slow=0, dac_code steps +1 per clock through all 256 codes, wrapping 255 -> 0.
- REQ-SLOW: with slow=1 the counter advances once every 256 clocks.
- REQ-SINE: in mode 10, dac_code walks the 256-entry table round(127.5+127.5*sin(2*pi*i/256)) (round half up), all entries exact.
- REQ-SWITCH: a mode change never produces a cycle where dac_code is neither the old source's value nor the new source's value.
- REQ-REGISTERED: dac_code changes only on a rising clk edge - a ui_in change between edges is not visible until the next edge.
- REQ-ECHO: uo_out always equals dac_code.

## Architecture

Single module r2r_dac_digital, one clock domain clk (20 ns). State: code_reg[7:0], cnt[7:0],
presc[7:0], dac_code register. Next dac_code = mux(mode: 00 -> ui_in, 01 -> cnt, 10 -> sine(cnt),
11 -> code_reg). Sine table is a combinational case statement. uo_out = dac_code (wire).
TT standalone pins: ui_in -> ui_in, mode -> uio_in[1:0], slow -> uio_in[2], dac_code -> uio_out[7:0]
(uio_oe driven by the wrapper), uo_out -> uo_out[7:0], rst_n -> rst_n (active-low, no inversion).

## Timing at the slow corner (accepted shortfall)

The block meets 50 MHz at tt and ff but not at ss / 125 C / 3.0 V. After
the last allowed fix (rst_n registered inside the block), the worst setup
slack there is -0.87 ns with max parasitics (-0.55 ns nominal, -0.29 ns
min). Hold is met at every corner. At tt the setup slack is +7.4 ns and at
ff +11.0 ns.

The worst path starts at `slow` (uio_in[2]) and ends at an internal flop.
Tiny Tapeout constrains inputs to arrive 20% of the period after the clock,
so that path needs a period of about 21.1 ns: the block meets roughly
47 MHz at ss / 125 C. That figure comes from the reported path, not from
a rerun of timing at 21.1 ns.

The owner accepted this on 2026-10-13. At 50 MHz the front end should
be correct at typical and fast silicon, and may miss setup on a hot,
slow die at low supply. Clock it at 47 MHz or below if that case matters.
