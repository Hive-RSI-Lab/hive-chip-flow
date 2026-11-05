# macro_net_not_dont_touch (top_harden)

The net on a macro's non-clock output was resolved from synthesis and passed
to LibreLane as `RSZ_DONT_TOUCH_RX`. But a placement or repair step's log
(the message names it) never reported the net as don't-touch, so that step
was free to buffer it. That is the #191 failure: the job runs out of memory
or the macro's pin ends up behind a buffer. Routes to `top_harden`.

**Cheapest fix first:** compare the net in the message with the step log's
`Net '...' matched don't touch` lines. A difference is usually escaping of a
bus bit (`q[0]` against `q\[0\]`) or a net renamed between synthesis and
placement. Both are engine bugs (`ttlib.dont_touch_rx`): report it with the
log. A top.override.json's `RSZ_DONT_TOUCH_RX` is added to the engine's
rule, not put in its place, so an override cannot drop the net.

**Trap:** this is not a timing finding. Never waive it. A net repair may
touch can make a later harden run out of memory.
