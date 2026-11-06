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
