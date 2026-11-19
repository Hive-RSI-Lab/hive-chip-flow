# fsm_coverage_below_threshold (cover)

A state machine has a state the visible suite never enters, or a
transition it never takes. The default floor is 100% (`cover.fsm_min`).
The message lists what was missed (`top.state::RUN->IDLE`). Routes to
`testbench`.

**Cheapest fix first:** add a test that reaches the source state and
drives the input that takes the missed transition, then checks where the
machine went. An abort, error or timeout arc is the usual one missed: the
happy path visits every state without ever taking it.

**Trap:** line and toggle coverage can both read 100% here. A transition
written as `state <= stop ? IDLE : DONE` is one line, and `stop` can
toggle in other states. Only the fsm count sees the abort arc.

A transition no test can take (a guard the design makes unreachable) goes
in `rtl/cover_exclude.yaml` with the label from the finding and a reason.
