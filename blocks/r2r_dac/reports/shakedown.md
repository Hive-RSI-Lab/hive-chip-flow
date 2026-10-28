# /msde shakedown: 8-bit R-2R DAC (3 rounds, 2026-09-25)

**The analog ladder is accurate but slow, and the tile never got built.**
DNL and INL pass at every corner. Settling misses 20 ns by a wide margin,
because no unit R meets both targets with buf_20 drivers at 3.3 V. The
digital front end, layout, LVS and TT precheck were not reached in 3
rounds, and /msde has no TT analog-tile path for them anyway.

## Measured (ngspice, ade sim_pvt, pre-layout)

Sizing: unit R = 27.0 kohm ppolyf_u (W 2 um, L 150 um), 2R from two
units, one trimmed unit (L 148.36 um) absorbs the driver Ron. Load
assumed at the pad: 100 ohm + 7 pF.

| corner | DNL max (LSB) | INL max (LSB) | monotonic | FS gain err (LSB) | glitch up / down (V.s) | INL, one unit +-1 % (LSB) |
|---|---|---|---|---|---|---|
| tt 27 C 3.30 V | 0.048 | 0.233 | yes | -0.005 | 1.8e-12 / 2.2e-15 | 0.526 |
| ss 125 C 2.97 V | **0.201** | **0.418** | yes | -0.017 | 1.6e-11 / 7.3e-13 | 0.717 |
| ff -40 C 3.63 V | 0.162 | 0.204 | yes | 0.001 | 4.1e-16 / 9.6e-16 | 0.523 |
| sf 125 C 2.97 V | 0.113 | 0.231 | yes | -0.010 | 1.2e-11 / 1.9e-12 | 0.539 |
| fs -40 C 3.63 V | 0.141 | 0.252 | yes | -0.001 | 8.3e-16 / 1.5e-15 | 0.563 |

- Full-scale range is v(255) - v(0) = 255/256 x VDD, about 3.287 V at tt.
- Settling to 0.5 LSB fails at every corner. The bench reads 99 ns, but
  that is its 100 ns window, not the real figure. The designer's estimate
  is tau = (27.1 kohm + 100 ohm) x 7 pF, about 190 ns, so settling takes
  about 1.2 us (about 59 periods at 50 MHz). I didn't measure that.
- The glitch energy is small because the slow RC filters the glitch.
- At the spec's original R = 250 ohm, the ladder settles in 19.3 ns at tt,
  but INL is 40.9 LSB and the output isn't monotonic.
- Measured buf_20 Ron at tt, 1 mA: pull-down 163 ohm, pull-up 444 ohm.
- Monte Carlo: the GF180 poly resistor models have no per-instance
  mismatch (ppolyf_u mis_r = 0, res_statistical is global only), so MC
  can't move DNL or INL. The one-unit +-1 % column stands in for it.
- Corners are ade's default five, not the brief's tt/ff/ss x -40/25/125 C
  at a fixed 3.3 V (breakage 3).

## Pipeline steps

Worked: task router (with a forced verb), msde split gate, ade spec_lint
(3 runs), topology, bench-writer, netlist designer, sim_tt, sim_pvt,
gate recording in state.json.

Broke:
1. /msde has no TT analog tile path. A 1x1 digital DEF only, no ua[]
   pins, and top_harden allows only interface pins + vdd + vss on the
   analog cell, so vout on ua[0] has nowhere to go. That blocks
   top_harden, top_drc, top_lvs and precheck. Left to chip-flow.
2. The router didn't match "full design of ... DAC"; `--verb full-run`
   was needed.
3. ade corners can't express tt/ff/ss x -40/25/125 C at a fixed VDD.
   Left to chip-flow.
4. The ade designer rule "never invent a device the template lacks"
   conflicts with msde's drivers-in-macro split, and the r2r_ladder
   template's bounds don't fit ppolyf_u.
5. sim_run's 60 s per-bench timeout is tight for 256-code benches on a
   loaded host. It didn't trip this round.
6. netlist_lint rejected the GF180 standard cells the macro needs, and
7. ignored `+` continuation lines on a `.subckt` header. Both are fixed
   in chip-flow PR #18. The recorded netlist_lint result still fails
   until that lands.
8. The step bench's fixed 100 ns window caps the settling readout, so a
   slow ladder can't report its real settling time.

Not reached: the digital front end (vde), layout, LVS, TT precheck.
cocotb in the eda image is 2.1.0, not the pinned 2.0.1.

## Cost

About $12 across 3 rounds: splitter $1.5, spec-writer $3, topology $0.4,
bench-writer $3.6, netlist designer $1.1, orchestration about $2.3.

## Round 2 (2026-09-25, chip-flow at #20)

### Analog, re-recorded on the #20 engine (pre-layout)

The owner kept the 27 kΩ unit R and accepted ~1.2 µs settling as a known
trade-off, so the settling bound is now 1.5 µs (9 ln2 τ, τ = 27k × 7 pF).
Corners are the brief's nine: tt/ff/ss × −40/25/125 °C at a fixed 3.3 V.
spec_lint, netlist_lint, sim_tt and sim_pvt all pass.

| measure | worst | corner | bound |
|---|---|---|---|
| DNL | 0.141 LSB | tt/−40 | 0.5 |
| INL | 0.355 LSB | ss/125 | 0.5 |
| monotonic | yes | all nine | |
| FS gain error | −0.014 LSB | ss/125 | ±0.5 |
| settling, 0.5 LSB | 1.441 µs rise, 1.436 µs fall | ss/−40 | 1.5 µs |
| 10–90 % | 0.506 µs | ss/−40 | 0.5 µs (warning) |
| INL with one unit ±1 % | 0.65 LSB | ss/125 | 0.5 (warning) |
| supply current peak | 22.8 mA | ff/−40 | 30 mA |

Glitch energy reads below 32 fV·s at every corner, but I don't trust it:
the glitch windows are 60 ns against a 190 ns time constant, so the
band method probably under-reads. That bench wasn't revised this round.

### Breakages found in round 2

9. top_harden treated every interface signal as one pin, so an 8-bit
   bus into the analog macro was refused. Fixed in chip-flow PR #21.
10. check_mutate puts every `-mode inv` mutant in the must-kill class,
    including flips of an internal prescaler bit that no port can see,
    so mutate is red at a 0.913 kill rate (0.9 needed).
11. Two stacked `# req:` lines above a cocotb test keep only the last
    one, with no warning.
12. Edits to `holdout/` have no edit class, and resume re-hashes them
    silently.
13. `state.py set-phase` passes a recorded FAIL with a warning and never
    checks H1 (read in the code, not demonstrated).
14. The router names the nested digital block `digital` (the directory),
    not the tile.
15. The cocotb 2.0.1 pin can't be honoured: the image has 2.1.0 and
    nothing checks a pin.
16. A 256-code DC sweep hit sim_run's 60 s limit at one corner on a
    loaded host. The bench's `sim_timeout_s` knob fixed it, which is the
    documented route, so this is a note rather than a bug.
17. bench_strength refused to run because the tt baseline had a
    warning-level miss (INL sensitivity 0.526 LSB), although sim_tt passes
    with it. A baseline warning would also have counted as a kill for
    every mutant. Fixed in chip-flow PR #22.
18. The layout step's `finalize()` rebuilds each polygon from its outline
    and drops holes. The PDK filltie's NPLUS is a keyhole around its P+
    tap, so after `finalize()` NPLUS covers the tap: 96 of the analog
    DRC's 98 findings (DF.16_MV, DF.3b, NP.3d/e, PP.3d/e on 16 fillties).
    The raw PDK filltie passes the same deck with none.
19. Block-level analog DRC runs the dummy-fill decks (2 × DCF.1a) although
    it drops density for the same reason, that a lone block has no fill.
20. The nested analog workspace was named `analog`, the directory, the
    same root as 14. layout_gen, analog LVS, pex_sim and top_harden all
    name files after it, so LVS looked for `netlist/analog.cir`. I
    renamed the block to `r2r_dac` in state.json to get past it (recorded
    as a decision); chip-flow should make the router use the msde naming.
21. Analog LVS can't match standard cells: `finalize()` flattens the eight
    buffers into 480 transistors, and the reference keeps them as black
    boxes. netgen also black-boxes `ppolyf_u`, so resistor sizes are never
    compared. The LVS gate is recorded FAIL for this.
22. gate.py ignores `--out` on its error path, and gates that build layout
    need the toolchain's Python (`bin/eda python`), not the host's, but the
    recipe prints host `python3`.
23. top_harden's power grid fails on the analog tile. PR #21's bus binding
    worked and the macro was placed (207.8 × 139.6 µm at 69.4, 94.1 in the
    346.6 × 325.4 µm 1x2 tile). LibreLane then stopped at
    `openroad-generatepdn`: [PDN-0179] Unable to repair all channels, with
    Metal1 channels left of the macro at x 3.4–59.4 µm. It's the engine's
    PDN setup for a macro in the tile (`macro_pdn.tcl`,
    `FP_PDN_MULTILAYER 0`); I didn't try to fix it. top_drc, top_lvs and
    the TT precheck can't run without the top GDS.

### Where round 2 stopped

| step | result |
|---|---|
| spec_lint, netlist_lint, sim_tt, sim_pvt (analog) | pass on #20 |
| split (msde) | pass |
| digital lint, sim, holdout, formal, cover | pass |
| digital mutate | fail, 2 unobservable mutants (10); waiver asked, unanswered |
| bench_strength (analog) | pass, 33 of 33 mutants killed, run on PR #22 and recorded |
| analog layout generator | builds (208 × 140 µm, 25 ppolyf_u + 8 buf_20) |
| analog DRC | fail, 98 (18, 19) |
| analog LVS | fail (21) |
| pex_sim | refused, no post-layout bench |
| top_harden | fail at PDN (23) |
| top_drc, top_lvs, precheck | not reached |
| digital standalone harden | not run, held on the mutate waiver |

Round 2 cost about $9: the bench-writer $0.6, the digital agent $1.5,
the layout agent $1.4, and orchestration for the rest.

## Round 3 (2026-09-27 to 2026-10-13, chip-flow main)

### Breakages found in round 3

24. top_harden's power grid still stopped at PDN-0179 on the first run.
    The later engine (#25 fix) got past it once the macro drew its
    supplies on Metal3, because the tile's Metal4 grid joins a macro
    only on Metal3. The generator only drew Metal1/2 supplies, so that
    was a real layout finding, and the layout fixer added the straps.
25. check_harden regenerates `harden/config.json` and `info.yaml` on
    every run, but the engine hashes its inputs at the start, so a
    first harden (or any run that changes them) records STALE. A rerun
    with identical files records fresh.
26. pex_sim exits 2 ("Too few parameters for subcircuit"):
    `check_pex_sim.reorder_pins()` rewrites only the first line of the
    extracted `.subckt` header and leaves magic's `+` continuation, so
    the netlist declares 16 pins. This blocks analog pex_sim, analog
    release, cosim and the msde release.
27. layout_gen writes the GDS into `layout/`, a hashed input of drc,
    after the start hash, so a first drc after deleting the GDS records
    stale (same class as 25).
28. `state.py restore` doesn't delete files that a fix created, so a
    restored fix can leave its override behind.
29. state.py has no verb to grant extra fix budget, so I recorded the
    owner's grant as a decision and edited the budget.
30. After a release with a waived FAIL, attest still says "blocked",
    and set-phase refuses the waived FAIL without `--force`.
31. `layout_gen.write_abstract()` reads only metal1/metal2 labels, so
    the abstract drops vdd/vss once they move to Metal3.
32. top_harden's pre-check could flag pins buried inside a macro before
    a 21-minute run. It didn't, so the buried dac_code pins cost a run.
33. top_harden's macro LEF (`check_top_harden.write_lef`, magic
    `lef write` with no `-pinonly`/`-hide`) exports each pin's whole
    connected net as PIN geometry. With the dac_code pins moved to the
    macro edge, routing DRC is 0, but the router still dropped Via1 onto
    dac_code's internal Metal1 about 30 µm inside the macro. That gives
    23 magic DRC (M1.2a, M2.2a, M1.3) and 73 LVS errors (dac_code shorted
    into the macro's vdd). `-pinonly` alone gives a 10 nm pin because
    the layout labels are points, so the fix also needs pin-sized label
    shapes. Reported, not worked around.
34. pex_sim (`check_pex_sim.py`) runs ngspice with a fixed 120 s
    timeout and takes no `--timeout`, unlike sim_tt and sim_pvt. Fix 26
    got pex_sim through extraction, but the DAC's RC-extracted bench
    needs 5 min 36 s, so the gate errors out. Run by hand, the same
    bench lands every measure inside its bound: settling 1.20 µs rise
    and fall (bound 1.5 µs), 10-90 % edges 424/422 ns (bound 500 ns),
    glitch energy 23.8/14.9 pV·s (bound 260 pV·s), glitch peak
    21.8/22.5 mV (bound 52 mV). Reported, not worked around.

35. top_harden places the analog GDS at the wrong offset. The macro's
    bbox starts at (-2.04, -3.2) µm because its vss Metal3 strap sits
    below y = 0, so the LEF has `ORIGIN 2.040 3.200`. OpenROAD puts the
    LEF's lower-left corner at the DEF point and routes to the pins
    there, but the final GDS puts the cell's (0, 0) at that point. Every
    pin and the vdd rail land 2.04 µm left and 3.2 µm below the wires
    that should reach them, so the 12 LVS errors are dac_code[7:0], vout
    and vdd left open. Fix 33 worked: magic DRC, routing DRC and antenna
    are all 0, and LVS fell from 73 to 12. Reported, not worked around.
    Fixed in chip-flow c4be2a4: top_harden now passes (82 s).

36. top_lvs refuses a real macro as a blackbox. Its guard counts the
    transistors in the macro's .subckt but skips standard cells, and the
    R-2R .subckt holds only eight gf180mcu buf_20 drivers and 16 ppolyf_u
    resistors. So the schematic side counts 0 while the extraction finds
    480 (the drivers' own transistors), and the gate exits 2 before
    netgen runs. The guard should count the same things on both sides
    (std-cell transistors included, or resistors too). Reported, not
    worked around. Fixed in chip-flow 0edfee5: the guard now counts every
    device, wants 480 fets + 25 res and gets the same, and netgen says
    "Circuits match uniquely".

37. Nothing fills the analog macro, and the TT precheck fails on it.
    ade's analog drc gate skips the dummy-fill decks because "the chip's
    fill step adds it", but top_harden's fill stops at the macro. So
    top_drc and the real TT precheck both report 2 x DCF.1a (the space
    between COMP must hold dummy COMP), and both regions are inside the
    macro. The fix is a fill step for the macro area (in top_harden or
    the analog layout path), or the gate has to check the fill rules
    early. Reported, not worked around. Fixed in chip-flow a00b866:
    top_harden fills the macro with COMP_Dummy, and top_drc and the TT
    precheck pass.

A note, not a breakage: msde release first failed on an open issue from
the 33/35 top_harden failures. The engine doesn't close an issue when its
gate later passes after an engine fix, so I marked it fixed by hand
(`state.py issue --status fixed`) and release passed.

### Where round 3 stands

| step | result |
|---|---|
| digital: all gates, release | pass; timing waived by the owner (-0.87 ns, about 47 MHz met at ss/125 °C) |
| digital H2 | asked (H2-a7bfe1), unanswered |
| analog: spec_lint to bench_strength, drc, lvs, release | pass (re-run fresh on 84ea5df) |
| analog H2 | asked (H2-4da295), unanswered |
| analog pex_sim | pass (34 fixed by a spec `sim_timeout_s: {pex_sim: 600}`; run takes 297 s) |
| top_harden | pass on a00b866 (35 and 37 fixed; harden's own DRC, LVS and antenna clean, setup slack +0.29 ns) |
| top_drc | pass on a00b866 (37 fixed: the macro is filled) |
| top_lvs | pass on a00b866 (fix 36): circuits match uniquely, macro 480 fets + 25 res on both sides |
| precheck | pass on a00b866 (TT's own klayout DRC clean) |
| cosim | pass (integrator bench: released RTL + ideal-driver real ladder; DNL/INL 0.006 LSB, settling 1.19 µs) |
| msde release | pass |
| msde H2 | asked (H2-1452e6), unanswered |
| design document | waits on H2 |
