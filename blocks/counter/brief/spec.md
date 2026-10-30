
## How it works

This project is an 8-bit programmable binary up-counter, in the style of a 74-series bus
counter. It has an asynchronous reset, a synchronous parallel load, a count enable, and a
tri-state output bus.

### The counter

The heart of the design is a single 8-bit register clocked on the rising edge of `clk`:

```verilog
always @(posedge clk or negedge rst_n) begin
  if (!rst_n)        count <= 8'h00;        // asynchronous clear
  else if (load & ~oe) count <= load_data; // synchronous load, bus released
  else if (enable)   count <= count + 8'd1; // count up
  // else hold
end
```

The four behaviours have a strict priority:

| priority | condition | effect |
| --- | --- | --- |
| 1 | `rst_n = 0` | count clears to `0x00` **immediately**, no clock edge required |
| 2 | `LOAD = 1` and `OE = 0` | on the next rising edge the count takes the parallel data D[7:0] |
| 3 | `ENABLE = 1` | on the next rising edge the count increments; 0xFF rolls over to 0x00 |
| 4 | neither | the count holds its value |

The reset is asynchronous because `negedge rst_n` is in the sensitivity list: pulling `rst_n`
low clears the count between clock edges, so the part comes up in a known state even with no
clock running. Everything else is synchronous — load and count only ever happen on a rising
edge of `clk`. Load beats count, so asserting `LOAD` and `ENABLE` together loads D and does
*not* add one to it.

### The bidirectional data bus, and why it is bidirectional

On a Tiny Tapeout tile only the `uio` pins can go high-impedance, through the `uio_oe` enable
byte; the `uo` pins are always driven. A genuine tri-state output therefore has to live on
`uio`, and an 8-bit tri-state output consumes all eight of them. That leaves only `ui` for the
control signals, and no room at all for a separate 8-bit parallel-data input port.

The resolution is the same one the 74-series bus counters use: **one 8-bit bidirectional bus
that is the output when the part is driving and the load input when it is not.**

| pin | direction | meaning |
| --- | --- | --- |
| `uio[7:0]` | bidirectional | `OE = 1`: `uio_oe = 0xFF`, the bus drives the count Q[7:0]. `OE = 0`: `uio_oe = 0x00`, the bus is released to Hi-Z and is sampled as the parallel load data D[7:0]. |
| `ui[0]` | input | `LOAD` — synchronous parallel load, active high |
| `ui[1]` | input | `ENABLE` — count enable, active high |
| `ui[2]` | input | `OE` — output enable for the tri-state bus, active high |
| `ui[7:3]` | input | unused; tie low |
| `uo[7:0]` | output | Q[7:0] mirror, always driven |
| `clk` | input | clock |
| `rst_n` | input | asynchronous reset, active low |

**The consequence to be aware of: because the load data arrives on the same pins as the output
bus, a load requires `OE` to be low.** If `OE` is high the tile is driving `uio`, nothing
external can drive data into it, and on GF180 the pad's input side is switched off while it
drives, so `uio_in` has no defined value. The design therefore ignores `LOAD` while `OE` is
high: the count holds, or keeps counting if `ENABLE` is also high. Drop `OE`
first, put D on the bus, then pulse `LOAD`.

So that the count is never invisible, `uo[7:0]` carries an always-driven copy of Q. You can
watch the counter on `uo` at all times, including while the `uio` bus is released.

`ena` is handled per the Tiny Tapeout convention: it is always 1 while the tile is selected and
it gates nothing in this design.

## How to test

Everything below is a sequence of pin drives you can follow by hand on the Tiny Tapeout demo
board, watching `uo[7:0]` on the LEDs.

**Setup.** Set `ui[7:3] = 0`. Use the demo board clock (or step it manually, which makes the
synchronous behaviour much easier to see).

1. **Asynchronous reset.** With the clock stopped, pull `rst_n` low. `uo[7:0]` goes to
   `0x00` right away, with no clock edge. Release `rst_n` high.
2. **Count up.** Set `ui = 0b010` (`ENABLE = 1`, `LOAD = 0`, `OE = 0`). Each rising clock edge
   advances `uo` by one: 1, 2, 3, ...
3. **Hold.** Set `ui = 0b000`. Clock it as many times as you like — `uo` does not change.
   Set `ui = 0b010` again and it resumes from where it stopped.
4. **Parallel load.** Keep `OE` low so the bus is an input. Drive the value you want on
   `uio[7:0]`, for example `0xA5`. Set `ui = 0b001` (`LOAD = 1`). Before the clock edge `uo`
   still shows the old count; on the first rising edge `uo` becomes `0xA5`. Clear `LOAD`.
5. **Load beats count.** Set `ui = 0b011` (`LOAD` and `ENABLE` both high) with, say, `0x77` on
   `uio`. The next edge gives `0x77`, not `0x78`.
6. **Wrap-around.** Load `0xFE`, then set `ui = 0b010`. Two clock edges give `0xFF` then
   `0x00` — the carry out of bit 7 is simply discarded.
7. **Tri-state output.** Set `ui = 0b110` (`ENABLE = 1`, `OE = 1`). `uio_oe` becomes `0xFF` and
   `uio[7:0]` now drives the same count you see on `uo[7:0]`. Clear `OE` (`ui = 0b010`) and
   the `uio` pins go high-impedance — an external pull-up or pull-down now wins the bus, while
   `uo[7:0]` keeps showing the count. `LOAD` is ignored while `OE` is high.

The CocoTB testbench in `test/` automates all of the above as eight separate tests:
asynchronous reset, synchronous load, counting from a loaded value, hold with the enable
deasserted, wrap-around, tri-state behaviour of `uio_oe`/`uio_out`, load priority over
count, and `LOAD` being ignored while `OE` drives the bus. Run it with `make -B` inside the `test` directory.

## External hardware

None. The design needs nothing beyond the Tiny Tapeout demo board: the input switches drive
`ui[2:0]` and, for a load, `uio[7:0]`; the output LEDs show the count on `uo[7:0]`. If you want
to see the high-impedance state on `uio` for yourself, a resistor pulling one of those pins to
VDD or GND is enough — it will win the pin whenever `OE` is low and lose whenever `OE` is high.
