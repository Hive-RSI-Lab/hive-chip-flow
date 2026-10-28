# Workarounds

Every place where chip-flow gets a block through its gates only because
something worked around a defect in the module, by a shim in `bin/` or by an
edit made by hand in a block's files. The score (`bin/chip-flow score`) reads
this table, and the test (`bin/chip-flow test`, and `bin/chip-flow check-all`
after its blocks) holds it to the truth:

- Each row's id has a probe, `tests/workarounds/<id>.py`, that exits 0 while
  the cause still stands and 10 once it is fixed. A probe whose cause stands
  with no row here fails the test, so removing a row without fixing the cause
  fails it. A row whose probe says it's fixed fails the test too: take the
  workaround out, remove the row, keep the probe as a guard, and the score
  goes up.

`steps` are the gate steps whose input the workaround made or changed, as
`<side>/<gate>` (`analog`, `digital`, or `top` for the block's own
workspace; a glob such as `digital/*` is allowed), or `-` for none. `blocks`
is `*` for every block, or a comma-separated list. A reviewer reads a probe
change as a change to the score.

| id | kind | blocks | steps | where | cause | brief |
|---|---|---|---|---|---|---|
