# mutate - score the test, never the design

Runs `mutate` alone: yosys mutates the current RTL (fixed seed; a
sample sized to the design by default - every point for a small one,
else one per 2 RTL lines, 20 to `mutate.max` - or `size: all` or a fixed
N; the report's `sampled_fraction` says how much of the design was
tried - inverted conditions, stuck outputs, removed resets, off-by-one
constants, swapped operators), and the visible suite plus bounded formal
run against each mutant. A mutant nothing catches "survived."

## Reading the result

Kill rate must clear 0.9, AND no survivor may land in a must-kill class
(`reset_removed`, `output_stuck`, `condition_inverted` -
`check_mutate.MUST_KILL_CLASSES`) regardless of the overall rate - a suite
that happens to kill 95% of mutants while missing every reset-removed one
has a specific, dangerous blind spot the aggregate number hides.

## Why each survivor lived

Every survivor that is not proven or ruled equivalent carries a `cause`
(facts `survivor_causes` and `survivor_cause_by_id`), which says what to
change in `tb/`:

- `not_activated`: line coverage of the unmutated design under the
  visible tests never reaches the mutated line. Add stimulus that runs it.
- `not_propagated`: the line runs, but the mutant's output ports match the
  original's in every test. Drive inputs that carry the change to an output.
- `not_detected`: an output differs (the finding names the port and the
  first time it differs), yet every test passes. Add a check on that port.
- `unknown`: a probe sim failed or timed out; the finding says why.

`causes_skipped` is set when the coverage run itself failed (every
survivor is then traced instead). A cause never changes the verdict.

## A mutant that hangs the tests

A test that waits for something with no bound of its own (`while
dut.valid.value != 1: await RisingEdge(dut.clk)`) waits forever on a
mutant that never raises it. Each mutant gets a time limit set from the
unmutated run's own time (facts `baseline_sim_s`, `sim_limit_s`: ten times
it, at least 60 s); one that runs past it is stopped and reported as
`timeout_<class>` (facts `timeouts`, `timeout_ids`). A timeout is never a
kill, so it lowers the kill rate. The fix is in `tb/`: wrap the wait in
`cocotb.triggers.with_timeout` (or count cycles and assert), so the test
fails on that mutant instead of hanging.

## This is the one gate whose failure is never the design's fault

A `mutate` failure means the TESTBENCH did not notice the design could be
wrong - full stop. `cluster_violations.FIXER_HINTS` already routes every
`survivor_*` kind and `kill_rate_below_threshold` to the `testbench`
domain; the fixer that picks up the work order edits `tb/`, never `rtl/`.
If you find yourself wanting to "just also improve the RTL a bit while
you're in there" - don't; that is scope creep into a different agent's
job and a different gate's evidence.

## Cost

The slowest single gate in the corpus (`docs/design.md` section 3) -
don't call this verb speculatively; let P4's own sequence run it once per
real RTL or testbench change.

## Survivors no test can kill, and the memory the proofs take

- Some survivors are dead cells: after `proc` folds a condition into a
  `$procmux`, the `$logic_not` that built it (`!busy`, `!rst_n`) has no
  fanout, yet mcy still lists and mutates it. These prove equivalent in about
  a second and are harmless.
- A mutant that is equivalent only because of the design's context cannot be
  killed by any test, and abc pdr runs out its share of the proof budget because
  induction fails. Seen: a constant top-level byte with a bit that is always
  0; a race that an 80,000-cycle debounce makes unreachable; a counter
  relabeling. Writing more tests does not help. The way out is the owner's
  ruling in `spec/mutant_rulings.yaml` (id, ruling, evidence); a ruled mutant
  skips the prover. An agent does not write its own ruling: escalate with the
  evidence and ask the block's owner.
- Each abc pdr proof can grow past 3.5 GB (sensor_counted, a 1024-cycle
  window counter, in 90 s). The gate runs at most one proof per
  PROOF_MEM_MB (4 GB) of memory headroom: the least of MemAvailable and
  every cgroup limit above it. A slice that is nearly full therefore runs
  the proofs one at a time, slowly but without being killed. Run `mutate`
  alone, not next to harden or another long job, and rerun it if it was
  killed anyway.
- The pdr proofs share one budget for the whole proof stage,
  `--proof-budget` (150 s by default). Each pdr proof gets the time left,
  split evenly over the survivors still unproven, and never more than
  `--pdr-timeout`. A proof the budget cuts is not a proof: the mutant stays
  a survivor, its reason names the proof budget, and the fact
  `proofs_cut_by_budget` lists those ids. To settle one, get the owner's
  ruling, or rerun with a larger `--proof-budget` when there is time.
- A design change renumbers the mutants. Rulings in `spec/mutant_rulings.yaml`
  keep their old ids, which now point at other mutants or none, and mutate
  refuses them ("not a mutant this run generated" or "that mutant is killed
  by the tests", each ending "remove the stale ruling"). Empty the rulings,
  rerun mutate, and ask the owner to rule the new survivors again.
- To check one survivor by hand, run `engine/scripts/mutate_runner.py` in a
  scratch task directory with `PRJDIR` pointing at a copy of the mcy
  database, or call `check_mutate.prove_equivalent(design_il, mutation,
  workdir)` through `bin/eda python`. `bin/eda` strips custom environment
  variables, so pass paths as arguments.
