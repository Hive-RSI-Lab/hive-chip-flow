# qor_regression (top_harden)

`top_harden` hardened the assembled top (`{ws}/top/`) and a metric came out
worse than the recorded top_harden it replaces
(`{ws}/reports/recorded/gate-top_harden.json`; the report's `qor_diff`) by
more than its threshold: slack/TNS/skew by 0.05 ns, area, utilisation, cells,
power and wirelength by 2 %, and any rise of a DRC, antenna, LVS or
timing-violation count that should stay 0. The top is compared only with its
own last top_harden, never with the digital side's harden. It is a warning
(the gate still passes) unless the digital spec sets
`harden_signoff.qor_regressions: error`. check-all prints the same diff under
the top_harden step.

**Cheapest fix first:** check `versions_changed` and `config_changed` in the
same `qor_diff`. A new tool or PDK version, an edited `top.override.json`, or
a new analog release (the macro's size or pins moved) explains most deltas.
If neither side changed, re-run top_harden once before blaming the design:
run-to-run routing noise is a few tens of ps.

**Trap:** never edit `top/` - top_harden rebuilds it from the two sides on
every run - and never raise a threshold to clear the diff. A deliberate trade
(a larger macro for a cleaner floorplan) is recorded as a design decision; the
next top_harden is compared with the new record.
