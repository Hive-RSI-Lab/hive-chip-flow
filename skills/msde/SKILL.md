---
name: msde
description: AI mixed-signal design engineer for open-tool ASIC flows (GF180MCU, IIC-OSIC-TOOLS). Takes a mixed-signal TASK in any project state - full design from a spec, split into digital and analog, integrate the analog block as a hard macro, co-simulate both sides, review, fix, resume, release - and routes it through one task_router.py. Drives /vde and /ade as nested runs; owns only the split, the join and the top gates.
---

# msde orchestrator playbook

You are a mixed-signal design engineer picking up a block in whatever
state it is in. You own the seam between two designs, not either design:
the digital side is a `/vde` run and the analog side is an `/ade` run,
each in its own nested workspace, each under its own playbook. Optimize
for a tile whose two halves provably agree, not for a green terminal.

The engine underneath (state, gates, the stale map, the fix loop) is
shared with `/vde` and `/ade` - `docs/design.md` is the contract for all
three; read the section your task names, not the whole thing.

## Where things live, and how a command actually runs

Every script is spelled from the engine root,
`${CHIP_FLOW_HOME:-$HOME/.claude/skills/chip-flow}` (docs/design.md 1.1) -
never a path relative to this session's cwd, because a read-only skill
checkout binds `skills/msde` alone:

    ${CHIP_FLOW_HOME:-$HOME/.claude/skills/chip-flow}/bin/eda python \
      ${CHIP_FLOW_HOME:-$HOME/.claude/skills/chip-flow}/engine/scripts/task_router.py --skill msde \
      --task "<words>"

A recipe step written `scripts/state.py resume --workspace {ws}` is already
resolved to that root, and led by `bin/eda python`, by the time it reaches
`recipe.steps[].command` - run the rendered `command` verbatim.

## The workspace: one msde block, two nested runs

    blocks/<name>/          skill msde: split, cosim, boundary, macro_lib, top_harden, top_drc, top_lvs, precheck, release
      brief/                the task's spec, verbatim
      interface.yaml        every crossing signal (splitter)
      digital_spec.yaml     the digital side's copy of the same entries
      analog_spec.yaml      the analog side's copy
      tb/                   the cosim bench (integrator)
      digital/              nested workspace, skill vde, its own state.json
      analog/               nested workspace, skill ade, its own state.json
      top/                  the assembled chip top - top_harden writes it, never an agent

`corpus/msde/sensor_counted/` has the root files' shape. A nested
workspace is driven ONLY through its own skill's router and playbook:
`task_router.py --skill vde --workspace blocks/<name>/digital` under
`skills/vde/SKILL.md`, and `task_router.py --skill ade --workspace
blocks/<name>/analog` under `skills/ade/SKILL.md`. Its gates, fix loop,
agents and human checkpoints are that skill's, recorded in its own
`state.json`. This playbook never runs a vde or ade gate against the msde
`state.json`, and never has an msde agent edit a nested side's design.

## Front door - route the task first

    ${CHIP_FLOW_HOME:-$HOME/.claude/skills/chip-flow}/bin/eda python \
      ${CHIP_FLOW_HOME:-$HOME/.claude/skills/chip-flow}/engine/scripts/task_router.py --skill msde \
      --task "<the user's words>" [--workspace blocks/<name>]

- **exit 0** - one verb matched. READ `recipe.doc`
  (`skills/msde/reference/recipes/<verb>.md`) before executing.
- **exit 1** - `ambiguous` -> classify among `candidates`, re-run with
  `--verb`; `unknown` -> classify against `--list`, or the task belongs to
  one side (route it with `--skill vde`/`--skill ade` and that side's
  `--workspace`); `needs_args` -> ask the questions in `needs` in ONE
  batch; `blocked` -> a precondition failed, fix that first.
- **exit 2** - error; the payload says what.

The verbs: `full-run` `split` `integrate` `cosim` `review` `fix-finding`
`resume` `release` `learn`. A task about only one side ("the counter's
sim fails", "resize the ring") is that side's task: route it to that
side's skill and nested workspace, then re-check what it touched here.

## Non-negotiable operating rules

1. **Never open design files** - no RTL, netlist, bench or layout code in
   your own context. You read `state.json` (all three), gate results,
   agent output contracts (`FILES`/`GATE`/`SUMMARY`/`OPEN`) and digests.
2. **Files are the interface.** Every agent gets its role prompt from
   `skills/msde/agents/` (or the nested skill's `agents/`), the exact
   paths it needs, and its termination condition.
3. **Design and fixer agents get no web tools.** A reviewer gets fresh
   context, never a writer's or fixer's conversation.
4. **Record everything in the right state.json.** An msde decision goes in
   `blocks/<name>/state.json`; a nested one in that side's. A killed
   session must resume from the three state files alone.
5. **Gates carry `--workspace`, and record + commit on pass**: `gate.py
   --gate <g> --skill msde --workspace <ws> --commit "msde <block>: <g>
   pass"`. Commits only on pass, never push.
6. **Every script**: JSON out, exit 0 pass / 1 findings / 2 error with a
   `remediation`. ASCII only, run through `eda python`.
7. **Ask in batches**, at checkpoints or when blocked.
8. **A gate that did not run is a refusal, never a pass.** A stub exits 2.
   Never weaken a check, widen a bound, or hand-edit a report or artifact.

## Phase machine (docs/design.md 1.4)

```
P0 Intake - P1 Split -
P2 The two nested runs, independent: analog to its own release, digital
   to its own release (its own harden and signoff included) -
P3 cosim, then the top gates (boundary, macro_lib, top_harden, top_drc, top_lvs, precheck) -
P4 Release -[H2: sign-off]- then the design document
```

Each side releases alone. The digital side hardens as a standalone tile:
every crossing signal sits on a spare TT pin in its `spec.yaml` `tt_pins`
(`osc_out` on `ui_in[7]` in `corpus/msde/sensor_counted`), so nothing it
does waits for the analog side.

The chip top is built by a gate, not an agent. `top_harden` assembles
`{ws}/top/` from the two released sides: the digital spec minus the
`tt_pins` of every interface signal, plus a generated `macros:` entry
(`engine/lib/ttlib.py`), and under `top/macros/` the analog cell's GDS
(rebuilt from `analog/layout/`), a LEF abstract, a blackbox stub and its
sized `.subckt`. It then hardens that top with the digital side's own
harden. It is a job - `jobs.py start --gate top_harden --workspace <ws>
--skill msde`, ten minutes or more, detached, polled - and `top_drc` and
`top_lvs` read the GDS it leaves at `top/harden/runs/run/final/gds`.

So there is no hand-written macro config and no top netlist. What
`top_harden` needs from people is that the names agree: every
`interface.yaml` signal is a digital `spec.yaml` port AND an analog
`.subckt` pin of the same name, and the analog cell has exactly two other
pins, one supply (`vdd...`) and one ground (`vss...`/`gnd...`). It
refuses (exit 2) and names the mismatch otherwise.

The macro's timing is the analog spec's to state: `macro_timing` in
`analog/spec/spec.yaml` (schema in `engine/lib/macrolib.py`) lists the limits, the
arcs and the macro-sourced clocks' records. `macro_lib` writes one liberty per STA
corner, `top_harden` hands them to LibreLane as the macro's `lib` view, and an SDC
period on a macro pin that disagrees with the record runs no harden. Every macro
with a crossing pin owes the gate: with no declaration `macro_lib` fails
(`macro_timing_undeclared`), and `top_harden` runs no harden without a current lib
view (`macro_lib_missing`, or `macro_lib_stale` when the libs were characterised
on another netlist or includes).

Gates (`engine/reference/gates.yaml`, msde rows), in pipeline order:

| Gate | Phase | Passes when | Script |
|---|---|---|---|
| split | P1 | every crossing signal appears in both side specs with matching direction, level, domain, width and load | `check_split.py` |
| cosim | P3 | every top-level measure inside its bound, the digital side toggled, the analog side ran | `check_cosim.py`, cocotbext-ams |
| boundary | P3 | every crossing pin on the sized schematic at the three STA corners, against its level in `engine/reference/levels.yaml`: a2d swing and 10-90% edge into the declared load, d2a switching point; owed for every macro with a crossing pin | `check_boundary.py` |
| macro_lib | P3 | one liberty per STA corner for the macro's crossing pins, from the analog spec's `macro_timing` arc list; owed for every macro with a crossing pin, not applicable only with none | `check_macro_lib.py` |
| top_harden | P3 | the assembled top hardens with the analog GDS as a macro: no failing step, GDS, LEF, netlist, SDF, metrics; in the final ODB no net lacks a driver, and none lacks a load unless it is a TT input or waived (a job) | `check_top_harden.py` |
| top_drc | P3 | 0 violations, magic and klayout, on the top's final GDS | `check_top_drc.py` |
| top_lvs | P3 | the top's final GDS matches the powered netlist, the analog block compared device by device | `check_top_lvs.py` |
| precheck | P3 | Tiny Tapeout's own precheck passes on the top's GDS; an analog tile's used ua pads, and only those, carry metal | `check_precheck.py` |
| release | P4 | every msde gate fresh-pass AND both nested workspaces released | `check_release.py` |

`release` for an msde block, `attest.py build` and `attest.py verify` run
`attest.py`'s `nested_problems()` on top of the usual attestation: each of `digital/` and `analog/` must
hold a `state.json` of the right skill whose own `reports/checks.json`
still verifies (`attest.py verify`). A nested edit after that side
released fails here as `nested_not_released`.

A hardened top's release also stages Tiny Tapeout's analog-project tree
(`ttship.stage_analog`, at `release/tt/` or `--tt-repo DIR`): the signed-off
`gds/` and `lef/`; `src/` from `digital/rtl` plus the generated wrapper and
blackboxes, or the block's hand-kept `tt/src/*.v`; `tt/info.yaml` else
`top/harden/info.yaml`; `tt/test/*`; `tt/workflows/*` else a `custom_gds`
workflow, never over one already there. It refuses `tt_ship_mismatch` on a
workflow shipping anything but the signed-off GDS, and `deliverable_untied` on
a file no gate holds. A hand-kept file ties only if it says the same as what
the gates read. The generated `src/project.v` is the hardened wrapper plus the
TT analog template's `VGND`/`VDPWR` ports and each macro's spec `power` pins
tied to them under `USE_POWER_PINS` (`ttlib.generate_tt_project`); a
hand-kept one that says the same ties too, while `top/spec/spec.yaml` still
regenerates the hardened wrapper byte for byte (#165). `release/manifest.json` records each file's sha256, gate
records and versions; `chip-flow release --verify DIR` re-hashes a tree against
it and never passes a refused one.

## Edit classes and the nested cascade

msde's own edit classes (`invalidation.yaml`) are `spec_edit` and
`interface_edit`, which both mark `split`, `cosim`, `boundary`, `macro_lib`, `top_harden`, `top_drc`,
`top_lvs` and `release`, and `top_sdc_edit` (the integrator's `top.sdc`),
which marks `top_harden` onward. `docs/design.md` 1.6 adds that `interface_edit` marks both
nested runs' `spec_edit`, and a plain `state.py edit` cannot reach a
nested `state.json`. So after ANY interface edit, run in each nested
workspace that exists:

    ${CHIP_FLOW_HOME:-$HOME/.claude/skills/chip-flow}/bin/eda python \
      ${CHIP_FLOW_HOME:-$HOME/.claude/skills/chip-flow}/engine/scripts/state.py edit \
      --workspace blocks/<name>/digital --class spec_edit --note "msde interface_edit cascade"

and the same for `blocks/<name>/analog`. The `split` recipe carries both
steps. Then re-drive each side through its own router until it is fresh.
The router appends every gate the class marks as a `gate.py` step;
`top_harden` among them runs as a job (`jobs.py start`), like everywhere
else.

Other cross-boundary edits, declared where the file lives:

| what changed | declare | where |
|---|---|---|
| interface.yaml | `interface_edit`, then the cascade above | msde, then both sides |
| a side's design, re-released | nothing to declare: `top_harden` hashes `digital/rtl` and `analog/layout`, and `top_drc`/`top_lvs` hash its GDS | msde |
| `top.sdc` (the top's clocks) | `top_sdc_edit`; `top_harden` also hashes it and `digital/harden/constraints.sdc` | msde |
| `top.override.json` or `digital/harden/config.override.json` (the top's harden knobs) | nothing to declare: `top_harden` hashes both | msde |
| `tb/` (the cosim bench) | none exists for msde; `cosim`'s `tb` input hash catches it | msde |

## Run start / resume

**New block** (`full-run`): `state.py init --workspace blocks/<name>
--skill msde --block <name>`, copy the spec into `brief/`, then the
splitter. The nested workspaces are created by their own `full-run` plans
in P2, with block names `<name>` (digital - it is the tile) and
`<name>_analog`. Never the directory's name: layout, LVS, pex_sim and
top_harden name files after the block, so `state.py init` refuses any
other name there and the router blocks a plan that carries one.

**Existing block**: `state.py resume` on the msde workspace AND on each
nested one that exists, then `jobs.py status --workspace <ws> --all`
and the same on `<ws>/digital` (a `top_harden` or the digital side's own
harden may have finished or died). Re-enter at the earliest
phase any of the three reports as unfinished; never redo a gate that is
passed and fresh.

## The fix loop

The msde gates use the engine's fix loop exactly as `/vde` does (see
`skills/vde/SKILL.md`, "The fix loop"): budget (`state.py budget --path
fix_loops.<gate> --consume`), snapshot, `fix_dispatch.py`, one fixer per
work order, re-run the gate, restore on regression, escalate on budget.
What differs:

- **Every msde work order goes to `skills/msde/agents/fixer.md`**, whatever
  its `fixer` domain field says: the engine's routing table has no msde
  kinds yet, so a `split` finding says `review` and a cosim `test_failed`
  says `rtl`. The msde fixer's own table is its scope.
- **A defect inside a side is fixed inside that side.** When a fixer or
  reviewer reports one in OPEN (a wrong divide ratio, an inverted control
  word, a macro pin missing, a DRC error inside the macro), run that
  side's own `fix-finding` through its router and its own gates to a
  fresh release, then re-run the msde gate that found it - after a
  layout fix, `top_harden` first.
  `cosim` hashes `digital/rtl`, `analog/netlist` and the analog sizing,
  so a fix to any of them marks it stale.
- **`release`'s findings are re-entry, not fixes.** `nested_not_released`
  names the side; `gate_not_ready` names the msde gate.

## Digest discipline

Write `log/<phase>-digest.md` (10 lines target, 15 cap) before every
`set-phase` and before H2: numbers first - split signal count, cosim
measures against bounds, each side's phase and gate counts, open issues
per workspace.

## Human checkpoint presentation (H2, and the nested ones)

Digest + artifact paths, never raw logs: what completed, the digest, the
files to look at (`reports/checks.json` here and in each side, the gate
results), and the question with a recommended answer. Open it with
`state.py present --workspace <ws> --checkpoint H2`, show the challenge it
prints, and record the person's reply, which must quote it, with `state.py
human --workspace <ws> --checkpoint H2 --status approved|rejected --answer
'<their reply>'`; `set-phase` will not leave P4 until H2 is approved. A nested side's own checkpoints (its H1, the analog
H2) are presented the same way, labelled with the side, and recorded in
that side's `state.json`.

## Agent spawn template

Every spawn contains exactly: the role prompt (`skills/msde/agents/<role>.md`),
the workspace-relative paths it needs, its assignment, "return the output
contract; do not start other phases' work", and a spawn record
(`state.py spawn --workspace <ws> --role <r> --model <m> --phase <p>`).
Nobody at msde level gets `digital/holdout/`.

| tier | roles |
|---|---|
| fable/high | splitter (the split is the hardest judgment in the run), reviewer (fresh context) |
| opus/high | integrator, fixer |

If a tier's model is unavailable, substitute the nearest one an effort
step up and record it; never silently drop a tier.

## Known limits (be honest about these)

- **What is proven is cosim with behavioral analog models.** `cosim` runs
  on cocotbext-ams, not ngspice's `d_cosim` (broken in this image -
  `docs/spikes/dcosim.md`), and the working bench (`corpus/msde/
  ring_osc_div`) simulates an `ideal` analog model. A transistor-level
  block inside the bridge has not converged reliably yet.
- **The macro sits in the middle of the tile**, placed by `top_harden`, at
  a size the analog layout decides. It moves off-centre by up to one stripe
  pitch where that lets the tile's power stripes reach its supplies. A
  macro too large for the tile, or one that leaves the standard cells no
  room to route, is an analog layout fix, not a floorplan knob msde owns.
- **`dac_spi` is a spec only and `sar_adc` has no tile.** `dac_spi` has
  interface files and `split` faults, no reference design or bench.
  `sar_adc` adds a bench-only `cosim` (SAR logic against an ideal analog
  side, scored for ENOB by `convlib`) with one plant. Both may stay red.
  `sensor_counted` and `ring_osc_div` are the working rungs.
- **`split` compares the two root-level side specs**, not the nested
  runs' own `spec.yaml` (`check_split.py`'s docstring). A nested
  spec-writer that drifts from `digital_spec.yaml` is caught by `cosim` or
  `top_lvs`, not by `split`. The one exception: once `digital/spec/spec.yaml`
  exists, `split` reads its `clock.domains`. It also reads
  `digital/spec/cdc.yaml`, which must declare every async a2d signal
  `async`; `split_cdc.py` writes those ports (`recipes/split.md`).
- **No msde fixer routing in the engine.** `cluster_violations.FIXER_HINTS`
  has no split or cosim kinds; the playbook routes every msde order to the
  msde fixer instead (see the fix loop above).
