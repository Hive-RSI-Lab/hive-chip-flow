# mutate - score the test, never the design

Runs `mutate` alone: yosys mutates the current RTL (fixed seed, N
mutants - inverted conditions, stuck outputs, removed resets, off-by-one
constants, swapped operators), and the visible suite plus bounded formal
run against each mutant. A mutant nothing catches "survived."

## Reading the result

Kill rate must clear 0.9, AND no survivor may land in a must-kill class
(`reset_removed`, `output_stuck`, `condition_inverted` -
`check_mutate.MUST_KILL_CLASSES`) regardless of the overall rate - a suite
that happens to kill 95% of mutants while missing every reset-removed one
has a specific, dangerous blind spot the aggregate number hides.

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
  killed by any test, and abc pdr runs to its 30 minute limit on it because
  induction fails. Seen: a constant top-level byte with a bit that is always
  0; a race that an 80,000-cycle debounce makes unreachable; a counter
  relabeling. Writing more tests does not help. The way out is the owner's
  ruling in `spec/mutant_rulings.yaml` (id, ruling, evidence); a ruled mutant
  skips the prover. An agent does not write its own ruling: escalate with the
  evidence and ask the block's owner.
- Each abc pdr proof can grow to about 2 GB, and the gate runs as many in
  parallel as it has mutate jobs. With a shared memory slice already busy
  (a harden running alongside) the job was killed for memory. Until the jobs
  cap covers the proof phase (issue #192), run `mutate` alone, not next to
  harden or another long job, and rerun it if it was killed.
- To check one survivor by hand, run `engine/scripts/mutate_runner.py` in a
  scratch task directory with `PRJDIR` pointing at a copy of the mcy
  database, or call `check_mutate.prove_equivalent(design_il, mutation,
  workdir)` through `bin/eda python`. `bin/eda` strips custom environment
  variables, so pass paths as arguments.
