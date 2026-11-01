# qor_regression (qor diff)

`qor diff` found a metric of the new harden worse than the old run's by more
than its threshold: slack/TNS/skew by `ns_abs` (0.05 ns), area, utilisation,
cells, power and wirelength by `rel` (2 %), and any rise of a DRC, antenna,
LVS or timing-violation count that should stay 0. Routes to `harden`.

**Cheapest fix first:** check `versions_changed` and `config_changed` in the
same report. A new LibreLane/yosys/OpenROAD or PDK, or an edited
config.override.json, explains most deltas; if the RTL did not change,
re-harden once before blaming the design (run-to-run routing noise is a few
10s of ps on small tiles).

**Trap:** do not raise the threshold to make the diff pass. A deliberate
trade (more area for slack) is recorded as a design decision, and the next
diff runs against the new recorded harden.
