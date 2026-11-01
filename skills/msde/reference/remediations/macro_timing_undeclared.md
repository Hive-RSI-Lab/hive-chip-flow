# macro_timing_undeclared (macro_lib)

A warning: `analog/spec/spec.yaml` has no `macro_timing`, so the macro's crossing
pins stay ideal ports to STA: no input caps on digital drivers, no real launch
slews. Release does not owe the gate.

**Fix:** declare it. `engine/lib/macrolib.py`'s docstring has the schema. Limits
are the spec's to state, nothing derives them.
