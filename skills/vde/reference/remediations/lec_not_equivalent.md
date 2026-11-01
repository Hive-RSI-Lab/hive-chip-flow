# lec_not_equivalent (lec)

A proof found inputs where two views differ, on the flop pin or output the
finding names, with the inputs in `counterexample`. `link` says which views.

**`synth_vs_final`:** P&R or an edit to `final/nl` changed logic. Nothing made
by the flow should: re-run harden, and if it comes back, look at what the config
lets the tools do. Never hand-edit `final/nl` to match.

**`rtl_vs_synth`:** read the RTL at that point first. A real difference is a
synthesis bug or an X in RTL the tool resolved. Moved state also lands here,
because this gate compares state by name. The usual case is a recoded FSM:
LibreLane always runs yosys's `fsm` pass, so a `state` flop can keep its name and
change encoding, and the data and output cones that read it then differ. Look for
`Recoding` in the synthesis log. If it's that, put
`(* fsm_encoding = "none" *)` on the state register in rtl/ and re-harden, so the
netlist keeps the RTL's encoding. Don't waive it.

`pin_kind` clock, set or reset with a cone that looks right means the flop type
changed (edge or reset polarity).
