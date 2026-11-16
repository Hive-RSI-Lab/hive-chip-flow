# lint_x_assign (lint)

An assignment whose right side holds an x (`assign z = 1'bx;`, `default:
y = 'x;`). Sim propagates the x; synthesis picks 0 or 1, whichever is
smallest, so sim and netlist differ, and an x reaching an output can hide
a real bug. **Fix:** assign the value the design should have. A genuine
don't-care that you want synthesis to optimise can be waived in
`rtl/lint_allow.yaml` with `rule: lint_x_assign`, the `file`, the `signal`
it names and a reason.
