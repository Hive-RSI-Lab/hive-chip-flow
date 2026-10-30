# counter: 8-bit programmable counter on a Tiny Tapeout GF180 tile

An 8-bit binary up-counter in the style of a 74-series bus counter: an
asynchronous reset, a synchronous parallel load, a count enable and a
tri-state output bus. The brief is `brief/spec.md` (the design's own
datasheet text, from ihsan-sa/ece298a-counter at `b2fd880`); this is the
same design, written down as requirements.

## Behaviour

One 8-bit count, priority reset > load > count > hold:

| priority | condition | effect |
| --- | --- | --- |
| 1 | `rst_n = 0` | count is 0 at once, with no clock edge, and stays 0 while `rst_n` is low |
| 2 | `LOAD = 1` and `OE = 0` | on the rising `clk` edge the count takes `uio_in` (D[7:0]) |
| 3 | `ENABLE = 1` | on the rising `clk` edge the count goes up by one; 255 wraps to 0 |
| 4 | otherwise | the count holds |

The count changes only on a rising `clk` edge, or when `rst_n` falls. Load
beats count: `LOAD` and `ENABLE` together load D, with no +1. While `OE`
is high the bus is an output, so `LOAD` is ignored then: the count holds,
or counts if `ENABLE` is high.

## The bus

Only the tile's `uio` pins can go high-impedance, so the tri-state output
is `uio[7:0]`, and the load data shares it. `uio_oe` is 0xFF while `OE` is
high and 0x00 while it is low, at all times; `uio_out` always carries the
count (the pads drive it only while `uio_oe` is set). `uo_out` carries the
count at all times, so it stays visible while the bus is released.

`ui_in[7:3]` and `ena` affect nothing.

## Interface

The block's ports are the tile's own, one to one:

| port | dir | width | meaning |
|---|---|---|---|
| ui_in | in | 8 | [0] LOAD, [1] ENABLE, [2] OE (all active high); [7:3] unused |
| uo_out | out | 8 | the count, always driven |
| uio_in | in | 8 | D[7:0], the load data, while OE is low |
| uio_out | out | 8 | the count, on the bus while OE is high |
| uio_oe | out | 8 | 0xFF while OE is high, else 0x00 |
| ena | in | 1 | unused |
| clk | in | 1 | clock, 50 MHz |
| rst_n | in | 1 | asynchronous reset, active low |

See `spec.yaml` for the requirements this maps to.
