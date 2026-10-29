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
- **Blocks**, `chip-flow check-all`: copies every block under this module's
  `blocks/` to a scratch dir and re-runs there, in pipeline order, every gate
  each of its workspaces records (an msde block's analog and digital sides
  first, then its top), then the probes. Hardens included, so it takes a
  while (about 20 minutes for r2r_dac): run it as a job on a change to the
  gates, `bin/` or a block. `--block NAME`, `--keep DIR` to read the logs
  after, and `--record` to write the run to the block's `check-all.json`;
  commit it.
- **Score**, `chip-flow score`, higher is better: how much of the known
  blocks passes every gate in the Hive. `blocks/known.json` pins them
  (ihsan-sa/ece298a-r2r-dac and ece298a-counter, each at the commit its
  block came from). Each scores the share of its `check-all.json` steps
  that that run passed (or failed under the block's waiver) and that no
  workaround names, while the block's files, `engine/` and `bin/eda` hash
  as the run had them (a change to any of them needs a new `--record` run,
  committed with it, or the block scores 0); the score is 100 x the mean. A known block not under `blocks/` yet, or with no
  record, scores 0, so bringing the counter in and taking it through its
  gates raises it, and so does any fix that gets a step to pass. It reads
  and hashes files, so it runs in seconds; check-all is what runs the
  gates, too slow for the Hive's 30-minute check.

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
