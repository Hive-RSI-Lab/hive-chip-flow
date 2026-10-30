# Designing a chip block with chip-flow in the Hive

chip-flow is ihsan-sa/chip-flow: three skills on one engine. `/vde` designs a
digital block in Verilog, `/ade` an analog block, and `/msde` takes a
mixed-signal task, splits it and drives the other two. Each takes a block
wherever it stands and ends at a GF180 Tiny Tapeout tile, and every result has
to pass gates: lint, simulation, formal proof, hardening, timing, DRC, LVS,
SPICE at every corner, and planted faults each gate must catch. The skills
are in [`skills/`](../skills/) and the engine in [`engine/`](../engine/),
kept as upstream has them ([what differs](upstream.md));
[`design.md`](design.md) is upstream's plan and contract. This page covers
what's different when an agent runs them inside the Hive.

## Before you start

- Run `chip-flow check` (`$HIVE_MODULES/hive-chip-flow/bin/chip-flow check`).
  Every row should be `ok`; `python-imports` can time out on a loaded machine
  (it is a 30 s wall limit), so read its detail before believing it.
- The tools are the Hive's IIC-OSIC-TOOLS 2026.09 tree, read-only at
  `/opt/eda-toolchain/iic-osic-tools-2026.09` (ngspice, xschem, magic, netgen,
  klayout, yosys, openroad, iverilog, verilator, sby, LibreLane, cocotb, the
  gf180mcuD PDK). `bin/chip-flow` finds it (an `iic-osic-tools-*` in
  `/hive/tools` first, then `/opt/eda-toolchain`; `EDA_TOOLCHAIN` wins) and
  runs everything through upstream's `bin/eda`, which runs the tree's
  binaries through its own loader and keeps its shims under your HOME. If
  `chip-flow check` can't find the tree, ask a warden; don't unpack one
  yourself.
- Blocks go in the project you work in, under `blocks/<name>/`. Commit the
  workspace on your branch like any other change; gate `--commit` isn't
  needed. LibreLane's `harden/runs/` stays out of git (upstream's
  `.gitignore`).

## Running it

- `eval "$(chip-flow env)"` sets `CHIP_FLOW_HOME` (this module) and
  `EDA_TOOLCHAIN`. The skills spell every command as
  `${CHIP_FLOW_HOME:-...}/bin/eda python ${CHIP_FLOW_HOME:-...}/engine/scripts/<x>.py`;
  with those set, run them as written. `chip-flow <script> [args]` is the
  same thing shorter, and `chip-flow eda <tool> [args]` runs any tool.
- Read the skill's `SKILL.md` (`skills/<vde|ade|msde>/SKILL.md`), then start
  from `chip-flow task_router --skill <skill> --task "<the ask>" --workspace blocks/<name>`
  and follow its recipe (`skills/<skill>/reference/recipes/<verb>.md`).
- A gate: `chip-flow gate --gate <name> --skill <skill> --workspace blocks/<name>`.
  It records itself into the block's `state.json`. Hardening runs long: the
  skills run it as a job (`jobs.py`), so don't wait on it in the foreground.
- The skill's subagents are the Agent tool: give each its role prompt
  (`skills/<skill>/agents/<role>.md`), the workspace path and the commands
  above. Reviewers always get fresh context.

## Sign-offs

The skills stop for the person at their `human` holds (architecture, spec,
release: `recipe.human_hold`). In the Hive each is a short packet to the
project's lead: the files to read on your branch, a digest of 10 lines or
fewer, and the exact question with a recommended answer. The lead puts it to
the person and sends back their answer; record it with `chip-flow state human`.
Don't use a warden request for a sign-off. If you go on while one is open,
say so in the packet, and rework if the answer is no.

## Every block gets its own docs

A block's `README.md`, at the root of its workspace, says what the block is,
the design decisions and why, how to rebuild and re-check it, and how to test
the silicon once it's back. It has to make sense to someone who has never
heard of chip-flow. Keep it current at every stage; `design_doc.py` writes
the longer design document from `state.json` and the recorded gate results.

## Be critical of the tools

The module improves the way the rest of the Hive does: by changing it, not by
working around it. When a script, check, prompt or recipe gets in the way or
gets something wrong (a false finding, a missing case, a role prompt that
left out a field, a path that only works on the old box), note it with the
file it lives in. Fix it in this module if it's small and yours; otherwise
file it as its own change: `hive agent brief --project hive-chip-flow ...`.
Upstream's rules hold: a gate that did not run is a refusal, never a pass,
and a check is never weakened to make a design pass ([`CLAUDE.md`](../CLAUDE.md)).

A workaround you can't avoid (a shim in `bin/`, or a hand edit in a block's
files that the flow should have made) gets a row in
[workarounds.md](workarounds.md) naming the gate steps it touched, and a
probe in `tests/workarounds/` that exits 0 while its cause stands and 10 once
it's fixed. That's what the score counts (`chip-flow score`), and the test
fails while a row and its probe disagree.

## Test and score

- **Test**, `chip-flow test`, every change: upstream's engine suite without
  its real-tool tests (`tests/check-engine.sh`, under a 120 s CPU budget)
  with the workaround probes (`tests/test_workarounds.py`), then the planted
  faults: every corpus rung whose faults need no harden, its untouched
  reference passing and each plant caught by its gate
  (`tests/test_corpus_faults.py`, real tools). Upstream's other real-tool
  sets still run by hand: `tests/check.sh` (a smoke per tool),
  `tests/check-slow.sh`, `tests/check-harden-faults.sh <rung>`, each with
  `EDA_TOOLCHAIN` set (`eval "$(chip-flow env)"`).
  It runs on GitHub Actions, not on the Hive's machine: the `test` job in
  `.github/workflows/test.yml`, on every pull request and every push to
  main, on an ubuntu-24.04 runner that unpacks the same pinned
  IIC-OSIC-TOOLS 2026.09 image (`hpretl/iic-osic-tools`, by digest) into a
  plain directory with upstream's `.github/actions/toolchain` and points
  `EDA_TOOLCHAIN` at it. `.hive/project.json` names that job in
  `forge_tests`, so the Hive waits on its result for a merge and takes its
  run on the merge commit as main's, and runs the test here only when CI
  is red or gives no answer.
- **Blocks**, `chip-flow check-all`: copies every block under this module's
  `blocks/` to a scratch dir and re-runs there, in pipeline order, every gate
  each of its workspaces records (an msde block's analog and digital sides
  first, then its top), then the probes. Hardens included, so it takes a
  while (about 20 minutes for r2r_dac, more on a loaded machine): run it as
  a job. Every step's outcome goes into the step cache, outside git
  (`$CHIP_FLOW_CACHE`, else `$HIVE_SCORE_CACHE`, else
  `~/.cache/chip-flow/steps`), keyed by the hash of what the step read.
  `--block NAME`, `--stale` for only the blocks with a step no run covers,
  `--budget S` to stop at S seconds, `--keep DIR` to read the logs after,
  and `--record` to also write the run to the block's `check-all.json`, a
  seed for a fresh cache. A PR needn't carry a record.
- **Score**, `chip-flow score`, higher is better: how much of the known
  blocks passes every gate in the Hive. `blocks/known.json` pins them
  (ihsan-sa/ece298a-r2r-dac, ece298a-counter and Hive-RSI-Lab/ece298a-pll,
  each at the commit its block came from). Each scores the share of its gate steps that a run on
  what that step reads now passed (or failed under the block's waiver) and
  that no workaround names; the score is 100 x the mean. A step's outcome
  is looked up by the hash of what it reads, in the step cache, then in
  the block's committed `check-all.json` (a seed). The score first runs
  `check-all --stale` to the end, so a stale known block is scored on its
  gates, not on whether the score had time, and a merge pays only for the
  blocks it changed, where the cache outlives the measure. A measure the
  Hive's 30-minute check cuts off keeps every step it finished in the
  cache, and the next try (60 minutes) runs only what is still uncovered;
  the planted-fault rungs then get `CHIP_FLOW_SCORE_FAULT_BUDGET` seconds
  (1500). A step that didn't run or finish counts as not clean, never as
  passed. `chip-flow score --no-run`
  reads only, in seconds. A known block not under `blocks/` yet scores 0.

## What it never does

It never submits. A Tiny Tapeout submission, a shuttle slot, or any order or
payment is the person's: stop at the released tile (`release` passed, the TT
project files staged) and say so. It never sends anything to a course
system either.

## The first block

[`blocks/r2r_dac/`](../blocks/r2r_dac/) is ihsan-sa/ece298a-r2r-dac's block,
chip-flow's first test block: an 8-bit R-2R DAC of GF180 poly resistors with
a small digital front end (code register, ramp, sine table) on one Tiny
Tapeout analog tile, released on the old box with the person's three H2
sign-offs. `chip-flow check-all --block r2r_dac` takes it through all its
gates again inside the Hive; its `README.md` says what each run found.

## The PLL

[`blocks/pll/`](../blocks/pll/) is Hive-RSI-Lab/ece298a-pll's `blocks/pll`
(the msde block the pll project is taking to a tile), copied in at the
commit `blocks/known.json` pins, so the score counts it and a module fix
that gets it through another gate raises the score. The copy leaves out
what no gate reads: `analog/log/` and `digital/log/` (117 MB of sim
scratch) and the `state_snapshots/`. Its harden config and flow SDC were
made portable the way the gates now write them (`ttlib.portable_config`,
`ttlib.flow_sdc_text`), the old box's absolute paths came out of its
reports, and the person's and host's names out of its rulings.

The pll project keeps designing it; this copy moves only when someone
brings it up to a newer commit: copy the block again the same way, set
the commit in `known.json`, and the score runs its gates (`chip-flow check-all --block pll` to see
them first). A block can't be scored from the other
repository without the copy: the score hashes the block's files as this
repository's git tracks them, and the check that measures it sees this
repository only. To drop the copy, `known.json` could name the source
project and its block's git tree id at the pinned commit, check-all would
take the block from that tree (`git archive`), and the record's `sources`
would carry the tree id in place of the files' hash; that needs the
other project's clone wherever the score runs.
