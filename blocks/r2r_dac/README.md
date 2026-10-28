# r2r_dac: 8-bit R-2R DAC on a Tiny Tapeout GF180 analog tile

chip-flow's first test block, from ihsan-sa/ece298a-r2r-dac at `4014bf6`
(coursework for ECE 298A). It's an 8-bit R-2R ladder of GF180 poly resistors
switched by standard-cell drivers at 3.3 V, with one analog output pin and
a small digital front end in the same tile: a code register loaded from
`ui_in`, a free-running ramp mode and a 256-entry sine-table mode, selected
by pins. The brief is [brief/spec.md](brief/spec.md); the design as released
and every number in it are in [docs/design.md](docs/design.md), and the
person's sign-offs are in [docs/signoffs.md](docs/signoffs.md) (both copied
from that repo, so their links point there).

It's an msde block of three workspaces, each with its own `state.json`:

- `analog/` (`/ade`): the ladder's netlist, sizing, generated layout
  (`layout/gen_r2r_dac_analog.py`) and benches; gates spec_lint,
  netlist_lint, sim_tt, sim_pvt, bench_strength, drc, lvs, pex_sim, release.
- `digital/` (`/vde`): the front end's RTL, testbenches, holdout and formal
  properties; gates spec_lint, lint, sim, holdout, mutate, formal, cover,
  synth, harden, timing, drc, lvs, glsim, precheck, release.
- the block itself (`/msde`): the interface between them and the hardened
  tile; gates split, cosim, top_harden, top_drc, top_lvs, precheck, release.

## Decisions carried with it

- Timing misses at ss/125 °C/3.0 V only: worst setup slack is −0.87 ns at
  50 MHz (max parasitics), about 47 MHz is met there, and tt (+7.4 ns), ff
  (+11.0 ns) and hold at every corner pass. After four fix attempts the
  person accepted it (`digital/reports/waivers.json`, `digital/state.json`
  decisions). The waiver is bound to the hardened design's hash, so it holds
  only while the harden it was approved over does.
- Two mutate survivors (prescaler bit inversions) are equivalent mutants:
  the prescaler is internal and its relabelling is unobservable at any port.
  The kill rate still clears the gate's 0.9 (0.913 on the old box), so no
  ruling was recorded.

## Rebuild and re-check

From the module: `bin/chip-flow check-all --block r2r_dac` copies the block
to a scratch dir and re-runs every gate each workspace records as passed,
hardens included, then reports per gate. One gate by hand, from the block's
parent: `chip-flow gate --gate <g> --skill <ade|vde|msde> --workspace r2r_dac/<side>`.
Gate logs (`log/`) and LibreLane's `harden/runs/` aren't committed; the
gates write them again.

## In the Hive

2026-10-28, the first runs on H0 (IIC-OSIC-TOOLS 2026.09 at
`/opt/eda-toolchain`, load around 110 on 20 cores):

- Every analog gate re-passed, release included (about 13 minutes).
- Every digital and top gate re-passed except three, all from the engine,
  none from the design: `mutate` errored on `mcy init`'s 60 s wall limit
  (sqlite waiting on a busy disk); `harden` marked itself stale on its own
  pass (its generated config carries absolute paths); and every re-harden
  changed the `harden` hash, so the timing waiver no longer matched and
  digital release, then top release, refused. The fixes are in
  [docs/upstream.md](../../docs/upstream.md). The re-harden reproduced the
  old box's netlist, LEF and DEF byte for byte.
- With those fixes, from a clean clone (no logs, no LibreLane runs), every
  gate re-passed again in 19 minutes, timing failing as waived. The waiver
  in that run was a test value in a scratch clone, bound to the new hash:
  digital release, then top release, passed under it.
- Under the new `harden` hash, the person's own waiver has to be approved
  again once, against the same numbers (`inputs_sha256` d5e263c9…4f55);
  that's theirs, and until then release refuses as it should.
