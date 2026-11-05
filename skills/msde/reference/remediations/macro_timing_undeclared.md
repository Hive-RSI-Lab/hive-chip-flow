# macro_timing_undeclared (macro_lib)

`analog/spec/spec.yaml` has no `macro_timing`. With crossing pins this is an
error: the macro's pins would stay ideal ports to STA (no input caps on digital
drivers, no real launch slews), so the gate fails, release owes it, and
`top_harden` runs no harden (`macro_lib_missing`). A macro with no crossing pin
gets a warning only, and release does not owe the gate.

**Fix:** declare it. `engine/lib/macrolib.py`'s docstring has the schema. Limits
are the spec's to state, nothing derives them.
