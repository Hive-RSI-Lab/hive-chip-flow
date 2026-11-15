# sim_unsupported (lint)

iverilog, the compiler the sim gate runs (cocotb's Icarus runner,
`-g2012`), printed "sorry: ... not supported" and refused the design.
`check_lint.py` runs it at lint so this surfaces here and not three gates
later; verilator and slang both accept the construct, which is legal
SystemVerilog, but sim cannot run it. Never allowlist-able.

**Cheapest fix first:** rewrite the named construct in the subset iverilog
reads. The common ones:

- `x inside {A, B}`: `(x == A) || (x == B)`; a range `[lo:hi]`:
  `(x >= lo) && (x <= hi)`.
- streaming `{<<{a}}`: a `for` loop (or a generate) that reverses bits.
- an unpacked `struct` (no `packed`): declare it `struct packed`, or use
  separate signals.
- `let`: a `function automatic` or a localparam expression.
- `assert property` / sequences in RTL: move them to the formal gate's
  property files (`formal/`); keep only immediate `assert` in RTL.

**Trap:** an iverilog "sorry" with exit 0 (it compiled past it, e.g.
"constant selects in always_* processes") is not a finding; only a design
iverilog refuses is. Don't chase those warnings.
