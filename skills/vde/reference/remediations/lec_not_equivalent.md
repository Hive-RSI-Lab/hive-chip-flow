# lec_not_equivalent (lec)

A proof found inputs where two views differ, on the flop pin or output the
finding names, with the inputs in `counterexample`. `link` says which views.

**`synth_vs_final`:** P&R or an edit to `final/nl` changed logic. Nothing made
by the flow should: re-run harden, and if it comes back, look at what the config
lets the tools do. Never hand-edit `final/nl` to match.

**`rtl_vs_synth`:** read the RTL at that point first. A real difference is a
synthesis bug or an X in RTL the tool resolved. A recoded FSM is not a reason:
the gate reads each map yosys's `fsm` pass wrote (`Recoding FSM` in the
synthesis log) and proves the recoded register through it, and the link's `fsm`
lists each map. A finding on `<register>.recoded[...]` is a transition or output
that differs through the map: a real bug. One with `applied: false` gives the
reason the map could not be applied. Don't add `fsm_encoding = "none"` to get
past it, and don't waive it.

A flop in `unmatched.gold` failing here is one synthesis removed that is not
constant: the gate proves a dropped flop constant by induction before it accepts
it, and lists the ones it accepts under `constants`. Look for a lost flop or
cone in the netlist.

`pin_kind` clock, set or reset with a cone that looks right means the flop type
changed (edge or reset polarity).

**`rtl_vs_synth` fails on a flop synthesis merged or removed (fix the RTL):**
the gate matches flops by name. A flop only the RTL side has is accepted when
it is proved constant by induction (`constants`) or when nothing observable
reads it. It still fails when synthesis merged two flops that something reads,
or removed a flop that is constant only because of the design's context (an
input range the proof does not know), and every output or flop D input that
reads it is reported as not equivalent although the design is correct. The
counterexample shows it: the RTL side reads the removed flop's Q as a free
input, and the netlist has a constant or the twin flop. Seen on a small tile,
six failures from four causes:

- two outputs from the same expression (a decimal point and an LED both
  `test | pwm`), which `opt_merge` made one flop;
- two outputs that are equal for every input (seven-segment `d` and `a` for
  every digit), merged the same way;
- an output vector bit that is `2'b00` but went through the output register,
  so it is constant;
- a shift register bit that reads another bit that is always 0 because every
  byte is 7-bit ASCII, or a stored byte whose bit 3 is always 0 because the
  byte is one of a few characters.

Fix in the RTL, behaviour unchanged: drive both outputs of a duplicate pair
from one flop; assign constant outputs directly, with no flop; do not store a
bit that is always 0 (make the register narrower and tie that bit in the mux).
Do not waive it or edit the netlist. The `cover` gate's toggle exclusions list
exactly the always-constant bits, so read that list before harden as a
checklist.
