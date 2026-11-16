# lint_ifdef_divergence (lint)

The same RTL builds a different port or flip-flop depending on which of
SYNTHESIS and FORMAL is defined. Sim and lint read it with neither, yosys
synthesis with SYNTHESIS, and formal with both, so each tool proves or
tests a different design, and only gate-level sim, an hour later, notices.
**Fix:** keep the `ifdef` inside the module and inside one register's
logic, so every view has the same ports and registers. A hand-placed
library flop under `ifdef SYNTHESIS` is fine when its output net has the
same name as the `reg` the other branch builds. A difference you really
mean (a sim-only checker register) is waived in `rtl/lint_allow.yaml`
with `rule: lint_ifdef_divergence`, its `file`, the `signal` the finding
names and a reason.
