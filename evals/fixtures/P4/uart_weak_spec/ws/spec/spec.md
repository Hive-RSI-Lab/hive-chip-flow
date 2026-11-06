# uart

A UART transmitter: one start bit, 8 data bits (LSB first), one further bit,
one stop bit, each held `CLKS_PER_BIT` clocks.

## Behaviour

- Idle: `tx` is 1, `busy` is 0. (REQ-IDLE)
- After power-up, hold `rst` high for a few clocks and the transmitter is
  idle. (REQ-RESET-INIT)
- A one-clock `start` pulse while idle with a byte on `data` begins a frame:
  start bit (0), then `data[0]` through `data[7]`, then one bit whose value
  this revision does not define, then the stop bit (1). Keep `data` steady for
  the whole frame. Each bit is held for `CLKS_PER_BIT` clocks. (REQ-START-BIT,
  REQ-DATA-LSB-FIRST, REQ-STOP-BIT, REQ-BIT-HOLD)
- `busy` is 1 from the start pulse until the stop bit's hold completes, then
  returns to 0. (REQ-BUSY-FRAME)
- Whenever `busy` is 0, `tx` is 1. (REQ-TX-IDLE-HIGH-WHEN-NOT-BUSY)

This revision says nothing about `rst` or `start` during a frame, so a test
must not drive either while `busy` is 1.

## Frame timing

A frame is 11 bits, so `busy` is high for exactly `11 * CLKS_PER_BIT` clocks
after the start pulse. Bit `k` of the frame (0 = start, 1..8 = `data[k-1]`,
9 = undefined here, 10 = stop) is on `tx` for clocks `k * CLKS_PER_BIT` to
`(k + 1) * CLKS_PER_BIT - 1` counted from the first clock of the frame.

`tx` and `busy` are registered outputs. A `start` sampled high on clock edge
N makes `busy` 1 and `tx` the start bit from edge N+1, the first clock of the
frame.

## Interface

| port  | dir | width | meaning |
|---|---|---|---|
| clk   | in  | 1 | free-running clock |
| rst   | in  | 1 | synchronous, active-high reset |
| start | in  | 1 | one-clock pulse: begin a frame |
| data  | in  | 8 | the byte to send |
| tx    | out | 1 | the serial line |
| busy  | out | 1 | high for the duration of a frame |

`CLKS_PER_BIT` is a module parameter (clocks per bit), default **4**
(`params.CLKS_PER_BIT` in `spec.yaml`). Clock: `clk` only, one domain, 10 ns.
