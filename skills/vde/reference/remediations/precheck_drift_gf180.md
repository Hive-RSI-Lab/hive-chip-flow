# precheck_drift_gf180 (CI: precheck-drift)

The vendored tt-support-tools pin is behind the `tools-ref` the shuttle's
precheck uses, and a path that can change gf180's result changed in between
(`engine/scripts/precheck_drift.py`). The message names the files and commits.
Its neighbours in the same report: `precheck_drift_diverged` (the pin is not an
ancestor of main), `precheck_klayout_out_of_range` (the image's klayout is
outside a klayout requirement) and `precheck_vendored_modified` (a vendored
file isn't the pin's).

**Cheapest fix first:** read the named commits' diffs for gf180, then follow
"Bumping the pin" in `engine/reference/tt/VENDORED.md`. A benign diff clears
with the bump and a re-record of the committed blocks.

**Trap:** a diff that changes a gf180 rule can make a committed block fail the
shuttle while every local gate still passes on the old pin. Bump the pin and
let the block fail precheck here, then fix the block. Never narrow `classify`,
edit a vendored file or pin to an older commit to clear the job.
