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
- `rst_n` is a synchronous, active-low reset: while low, on every clk the
  code register is 0, the counter is 0 and the slow prescaler is 0, so
  `dac_code` is 0 (ladder at zero scale).
- `ena` (the Tiny Tapeout enable) is accepted and ignored, as TT designs
  normally do.
- `dac_code` is also echoed on `uo_out[7:0]` so the code the ladder sees is
  observable at the digital pins and the digital side can be tested and
  released without the macro.

## Interface (top-level ports)

| port          | dir | width | meaning |
|---------------|-----|-------|---------|
| clk           | in  | 1 | free-running system clock, 50 MHz (20 ns period), the `clk_sys` domain |
| rst_n         | in  | 1 | synchronous, active-low reset |
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
