# signoff_metric_missing (harden)

LibreLane exited 0 but `final/metrics.json` has no number for a metric
gates.yaml's `harden_metrics` judges, so that check did not happen. It is not
a zero. The finding's `metric` names it. Routes to `harden`.

**Cheapest fix first:** see whether a step that writes the metric was skipped
or removed, through `harden/config.override.json` or a changed flow. Put the
step back and re-harden. If a LibreLane update renamed the metric, change the
name in `harden_metrics`, and check the new one means the same.

**Trap:** do not remove the row from `harden_metrics` to get a pass.
