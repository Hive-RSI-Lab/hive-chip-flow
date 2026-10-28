# What differs from ihsan-sa/chip-flow

Everything outside `module.json`, `README.md`, `bin/chip-flow`,
`bin/check_all.py`, `bin/score.py`, `blocks/`, `tests/workarounds/`,
`tests/test_workarounds.py` and the files `docs/README.md`, `docs/upstream.md`
and `docs/workarounds.md` is ihsan-sa/chip-flow at
`b9bf8716d86fcd0ffdcb24e289cea8aaa5c7c055` (main, 2026-10-15), at the same
paths, because the skills reach the engine as `$CHIP_FLOW_HOME/engine/...` and
`$CHIP_FLOW_HOME/bin/eda`, but for the [fixes the Hive made](#fixes-the-hive-made)
and `tests/conftest.py`'s `NOT_CARRIED` (an upstream test of a file left out,
skipped with its reason). Left out:

- `.github/` (upstream's CI, which unpacks the toolchain on GitHub's runners;
  here the test is `bin/chip-flow test`) and `.dispatch/` (upstream's run
  notes).
- `GOALS.md` (upstream's goals; this project's are `.hive/goals.md`) and
  upstream's `README.md`, which is `README.upstream.md` here.

The box that ran chip-flow before kept a copy pinned at `85aa174`, older than
this one; since then upstream added a planted fault for every gate (#82),
the ade, msde and vde planted faults on CI (#86, #88, #90), the riscv formal
fix (#92) and `tests/check-harden-faults.sh`.

## Fixes the Hive made

Found taking `blocks/r2r_dac` through its gates here, each worth sending
upstream:

- `engine/scripts/check_mutate.py`: `mcy init`'s wall-clock limit is 600 s,
  not 60. mcy keeps its mutants in sqlite, and on a disk shared with many
  jobs its commits wait on fsync: r2r_dac's init took 2 s on tmpfs and 116 s
  on the shared disk, with 0.13 s of CPU, and the gate errored.
- `engine/lib/statelib.py` and `engine/reference/invalidation.yaml`:
  `harden_config` hashes as `json_canonical_sans_roots` (the block's path,
  chip-flow's and PDK_ROOT as placeholders). check_harden.py rewrites
  `harden/config.json` with absolute paths on every run, so a block hardened
  anywhere but where it last was marked its own harden stale on its first
  pass, and release refused.
- The same two files: `harden` hashes as `harden_signoff`, the harden dir's
  own files and each run's `final/` outputs with export timestamps and the
  machine's roots taken out, not every file LibreLane leaves (`dir_text`).
  Re-hardened here, r2r_dac's netlist, LEF and DEF came out byte for byte
  as the old box's, and its GDS, SDF, SPEF and magic views differed only in
  those stamps; under `dir_text` every re-harden staled timing, drc, lvs,
  glsim, precheck and release and voided any waiver over them. This is the
  `gds_geometry` item upstream's invalidation.yaml left for later, done with
  the stamps rather than klayout. Existing blocks' recorded `harden` inputs
  read stale once (fail-safe), and a waiver over a gate that reads
  `harden` has to be approved again once.

## The two pull requests left open upstream

Neither is carried in:

- **#99** ("wip: run 5 timeout", closes upstream issue #98) is the
  "formal stops blaming healthy asserts" goal in upstream's GOALS.md
  (`check_formal.py` reading a pdr FAIL as explained by another assert's
  basecase counterexample, plus a remediation). Its run timed out before its
  check finished; nothing shows it passes. It belongs here as its own change
  to this module, finished and tested.
- **#94** lands the counter8 eval round: 18 paid headless Claude Code runs in
  a bubblewrap sandbox with its own proxy, ledger and spend caps. In the Hive
  that is the improve module's and the trials' job (the Hive's own sandbox,
  scores and spend accounting), so carrying a second harness for it in a
  design module would duplicate them. Its corpus briefs
  (`corpus/vde/counter8/spec.terse.md`, `spec.full.md`) can come in alone if
  a trial wants them.

## To update

Copy upstream's tree over this one, leaving out the files above, keep this
module's own files, and bump the commit above. Then `bin/chip-flow test` and
`bin/chip-flow check-all`.
