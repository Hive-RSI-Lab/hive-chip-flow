# qor_regression (qor diff, check_harden)

`qor diff`, or check_harden against the recorded harden it replaces
(reports/recorded/gate-harden.json, its `qor_diff`), found a metric of the new harden worse than the old run's by more
than its threshold: slack/TNS/skew by `ns_abs` (0.05 ns), area, utilisation,
cells, power and wirelength by `rel` (2 %), and any rise of a DRC, antenna,
LVS or timing-violation count that should stay 0. Routes to `harden`.
From check_harden it is a warning (the gate still passes) unless spec.yaml
sets `harden_signoff.qor_regressions: error`. check-all prints the same diff
under each harden step.

**Cheapest fix first:** check `versions_changed` and `config_changed` in the
same report. A new LibreLane/yosys/OpenROAD or PDK, or an edited
config.override.json, explains most deltas; if the RTL did not change,
re-harden once before blaming the design (run-to-run routing noise is a few
10s of ps on small tiles).

**Trap:** do not raise the threshold to make the diff pass. A deliberate
trade (more area for slack) is recorded as a design decision, and the next
diff runs against the new recorded harden.
