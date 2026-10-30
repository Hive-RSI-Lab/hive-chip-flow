# hive-chip-flow

The chip-flow module: ihsan-sa/chip-flow's chip design skills and their
engine, as a Hive module in the Hive org like hive-hwde.

An agent designs a chip block with open tools: `/vde` a digital block in
Verilog, `/ade` an analog block, `/msde` a mixed-signal task that drives the
other two. Every result passes gates that are hard to fool (lint, simulation,
formal proof, hardening, timing, DRC, LVS, SPICE at every corner, planted
faults each gate must catch) on its way to a GF180 Tiny Tapeout tile. The
person signs off between stages. Submitting a tile is always the person's.

- `skills/{vde,ade,msde}/`: the skills (playbook `SKILL.md`, agent roles,
  recipes, remediations); `engine/`: state and checkpoints, gates, the fix
  loop, jobs, the checks; `corpus/`: reference designs with planted faults;
  `evals/`: the ladder and scorecard. All as upstream has them
  ([what differs](docs/upstream.md)); upstream's own README is
  [README.upstream.md](README.upstream.md) and its plan [docs/design.md](docs/design.md).
- `bin/eda`: upstream's launcher, running the tools from the Hive's
  IIC-OSIC-TOOLS tree with no docker and no root.
- `bin/chip-flow`: finds that tree and runs the engine's scripts, the tools,
  the test, check-all and the score.
- How an agent uses it in the Hive: [docs/README.md](docs/README.md)
- The first block: [blocks/r2r_dac/](blocks/r2r_dac/), the R-2R DAC; and
  [blocks/pll/](blocks/pll/), the PLL the Hive is designing.

## Test and score

- **Test**, `bin/chip-flow test`, every change: upstream's engine suite
  without its real-tool tests (`tests/check-engine.sh`, held to 120 s of CPU)
  with the workaround probes (`tests/test_workarounds.py`), then every
  corpus rung's planted faults that need no harden, each caught by its gate
  (`tests/test_corpus_faults.py`).
- **Blocks**, `bin/chip-flow check-all`: re-runs every gate each block under
  `blocks/` records, from a scratch copy, through `bin/eda`, then the
  probes. Slow (it hardens). Each step's outcome goes into a step cache
  outside git, keyed by the hash of what that step read; `--stale` runs
  only the blocks with a step no run covers, `--budget S` stops at S
  seconds. `--record` also writes the block's `check-all.json`, a seed the
  score falls back on where the cache is empty.
- **Score**, `bin/chip-flow score`, higher is better: for the known blocks
  ([blocks/known.json](blocks/known.json): ihsan-sa/ece298a-r2r-dac,
  ece298a-counter and Hive-RSI-Lab/ece298a-pll at their pinned commits), the mean share of gate steps
  that passed a run on what each step reads now (the block's files,
  `bin/eda` and the engine files that gate reads), with no workaround
  listed in [docs/workarounds.md](docs/workarounds.md). It first runs, within
  a budget (`CHIP_FLOW_SCORE_BUDGET`, 1500 s), the steps no cached or seeded
  run covers; a step that didn't run counts as not clean. `--no-run` reads
  only. A known block not brought in yet scores 0.

MIT, like upstream (LICENSE); the vendored Tiny Tapeout templates and tools
under `engine/reference/tt/` keep their own licences (VENDORED.md there).
