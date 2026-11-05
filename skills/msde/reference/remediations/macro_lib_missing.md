# macro_lib_missing (top_harden)

The analog macro has crossing pins and `top_harden` found no lib view for them:
`analog/spec/spec.yaml` declares no `macro_timing`, or `macro_lib/` has no
report or no `.lib` for an STA corner. No harden ran: without the libs STA sees
the macro's pins as ideal ports and cannot judge the digital side's slews.

**Fix:** declare `macro_timing` if it is missing (schema in
`engine/lib/macrolib.py`), run the `macro_lib` gate, then `top_harden` again.
