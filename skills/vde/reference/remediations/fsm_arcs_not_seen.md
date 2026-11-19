# fsm_arcs_not_seen (cover)

Info severity, never a failure. Verilator found the state machine, but it
extracted no transition out of the named states. Verilator 5.052 reads a
transition only from a plain `if/else` or a single `?:` inside each case
item. An else-if chain or a nested `?:` is not counted, so fsm coverage says
nothing about those states' transitions. Line coverage still sees that
code.

To have them counted, write each such state's next-state logic as one
`if/else` or one `?:`. That is an RTL style change, made only when the
block's owner wants it. Otherwise, check those transitions with line
coverage and assertions. Do not exclude anything for this finding.
