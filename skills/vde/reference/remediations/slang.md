# slang_* (lint)

A finding from slang, the strict IEEE 1800 front end `check_lint.py` runs
after verilator. The kind is `slang_<name>` for slang's own `[-W<name>]`
(`slang_redefinition`, `slang_multiple-always-assigns`,
`slang_mixed-var-assigns`), or `slang_error` when it names none. slang
errors are never allowlist-able; slang warnings go through
`rtl/lint_allow.yaml` keyed by the same `slang_<name>`.

**Cheapest fix first:** slang's message is the standard's own rule, and
its file:line points at the second offender. A name declared twice in one
scope: delete one. A variable written from two always_ff blocks, or from
an always_comb and an `assign`: give it exactly one driving process and
merge the logic there.

**Trap:** verilator often reports the same fault only as a warning
(MULTIDRIVEN, for both the always_ff and the always_comb case). Allowlisting the verilator
warning does not silence slang, and should not: tools later in the flow
(synthesis, equivalence) read the design the strict way.
