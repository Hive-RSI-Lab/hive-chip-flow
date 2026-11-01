# qor_metric_missing (qor)

A metric of the QoR schema (engine/lib/qorlib.py) is not in the run's
`final/metrics.json`, or the old run had it and the new one does not. It is
reported missing, never read as 0. Routes to `harden`.

**Cheapest fix first:** the step that writes the metric did not run - look
in the run directory for the skipped or failed step (STA corners, IR drop,
antenna check), or a config override that turned it off (RUN_IRDROP_REPORT,
STA_CORNERS). Re-harden with the step on.

**Trap:** a LibreLane upgrade can rename a metric key; then fix the key in
qorlib.SCALARS/PER_CORNER, don't drop the metric from the schema.
