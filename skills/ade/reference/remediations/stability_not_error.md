# stability_not_error (spec_lint)

The `pm_deg` measure of a `feedback: true` block has `severity: warning`.
A phase margin below its bound is an unstable or ringing loop, so the
bound is an error.

**Cheapest fix first:** drop the `severity` key (it means `error`).

**Trap:** a warning-only margin never fails a gate, so a design with no
compensation cap would release.
