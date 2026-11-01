# lec_not_equivalent (lec)

A proof found inputs where two views differ, on the flop pin or output the
finding names, with the inputs in `counterexample`. `link` says which views.

**`synth_vs_final`:** P&R or an edit to `final/nl` changed logic. Nothing made
by the flow should: re-run harden, and if it comes back, look at what the config
lets the tools do. Never hand-edit `final/nl` to match.

**`rtl_vs_synth`:** read the RTL at that point first. A real difference is a
synthesis bug or an X in RTL the tool resolved. Retiming or a recoded FSM also
lands here, because state moved: this gate compares state by name, so turn that
synthesis option off rather than waive it.

`pin_kind` clock, set or reset with a cone that looks right means the flop type
changed (edge or reset polarity).
