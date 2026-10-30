# counter: 8-bit programmable counter on a Tiny Tapeout GF180 tile

chip-flow's second known block, from ihsan-sa/ece298a-counter at `b2fd880`
(coursework for ECE 298A). It's an 8-bit up-counter like a 74-series bus
counter: an asynchronous active-low reset, a synchronous parallel load, a
count enable and a tri-state bus on `uio[7:0]` that is the count's output
while `OE` is high and the load data while it's low, with an always-driven
copy of the count on `uo_out`. The brief is [brief/spec.md](brief/spec.md)
(the design's own `docs/info.md`); [spec/spec.md](spec/spec.md) and
[spec/spec.yaml](spec/spec.yaml) are the same design as requirements.

It's a `/vde` block, one workspace: gates spec_lint, lint, sim, holdout,
mutate, formal, cover, synth, harden, timing, drc, lvs, glsim, precheck,
release.

## How it came in

That repository is a plain Tiny Tapeout project, not a chip-flow
workspace, so it was brought in by hand on 2026-10-29 rather than through a
`/vde` full run:

- `rtl/ece298a_counter.v` is its `src/project.v` with the module renamed
  from `tt_um_ece298a_counter` (chip-flow generates the `tt_um_` wrapper
  from `tt_pins`, and that wrapper keeps the name) and the copyright line
  pointing at the repository. The ports are the tile's own, one to one.
- `tb/test_ece298a_counter.py` is its `test/test.py`: the eight tests,
  each tagged with the requirements it covers, `SETTLE_NS` 10 ns rather
  than 1 so a gate-level run's clock-to-output delay settles, and one test
  added for REQ-UNUSED (`ui_in[7:3]` and `ena`).
- `holdout/` and `formal/` are new: a reset with no clock running, a load
  of every value, a full lap with the bus driven and OE turned around on
  every edge; and a proof of each requirement but REQ-UNUSED (the async
  reset turns on sby's multiclock).
- The tri-state bus needs `uio_oe` to follow `OE`, so `tt_pins` can now
  map a port onto `uio_oe` (engine/lib/ttlib.py); before, the wrapper
  always tied it to a constant.

Nobody has reviewed it at H1 or signed it off at H2: those are the
person's, and the workspace's phase stays P0 until they do.

## In the Hive

2026-10-29, the first runs on H0 (IIC-OSIC-TOOLS 2026.09, load 300 to 440
on 20 cores): every gate passed, release included. Formal proves all six
properties (smtbmc and abc pdr); mutate kills 21 of 21 scored mutants (2
more proven equivalent); cover is 100% of lines and 97.6% of toggles (the
3 left are `ui_in[7:3]` bits never falling, which REQ-UNUSED covers in
sim); worst setup slack is +7.12 ns at ss/125 C/3.0 V for 50 MHz, worst
hold +0.49 ns at ff. The first harden in a fresh workspace recorded its
generated `config.json` and `info.yaml` as absent (they didn't exist when
it started), so release read it as stale until harden ran a second time.

## Rebuild and re-check

From the module: `bin/chip-flow check-all --block counter` copies the
block to a scratch dir and re-runs every gate `state.json` records,
harden included; `--record` writes `check-all.json`, which the score
reads. One gate by hand: `bin/chip-flow gate --gate <g> --skill vde
--workspace blocks/counter`. Gate logs (`log/`) and LibreLane's
`harden/runs/` aren't committed; the gates write them again.
