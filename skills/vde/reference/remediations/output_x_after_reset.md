# output_x_after_reset (xprop)

SymbiYosys found an input sequence after which an output can be X even
though the reset was held for `formal.reset_cycles` clocks. The design was
encoded with yosys `xprop` (every bit carries a "could be X" twin), every
register starts X, every input is defined, and the counterexample names the
port and the step. Routes to `rtl`.

**Cheapest fix first:** open the trace under `log/xprop/run` and follow the
port back to the register the reset does not set (a reg with no `if (rst)`
arm, a memory read before it is written, or a write enable that is X on the
first reset clock because it reads a state register before reset lands).
Reset that register, or gate the enable with `!rst`.

**When the X is real but harmless:** a RAM or register file that is read
before it is written on purpose (a CPU's registers, an unloaded program
memory) goes in spec `formal.x_init_ok` with a reason; the proof then gives
it an arbitrary but defined power-up value. An output that is allowed to be
X while unused goes in `sim.x_ok` with a reason. There is no waiver: an
entry that hides a real X fails review, and a name that matches nothing in
the design is refused.

**Replay:** `bin/eda python engine/scripts/check_xprop.py --workspace <ws>`;
the wrapper and the sby script are in `log/xprop/`.
