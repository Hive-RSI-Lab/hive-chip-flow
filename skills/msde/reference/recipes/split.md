# split - one interface, two specs that agree with it

`split` is P1 of a full run, and the verb for any later change to what
crosses between the two sides.

## What the splitter writes

- `interface.yaml` - `signals:`, one entry per crossing signal, each with
  `name`, `direction` (`a2d` or `d2a`), `level`, `domain`, `width` and
  `load`. Every field is required; `check_split.py` refuses (exit 2) an
  entry missing one rather than compare nothing with nothing.
- `digital_spec.yaml` and `analog_spec.yaml` - each an `interface:` list
  with the same entries, plus whatever that side's own spec needs.
- On a Tiny Tapeout analog tile, `interface.yaml`'s `ua_pins:
  {<pin>: <k>}` maps every analog pad to `ua[k]`, and `analog_spec.yaml`'s
  `pads:` lists the same pins. A pad is never in `signals:`: top_harden
  and precheck route pads only from `ua_pins` (`corpus/msde/dac_tile/`).
- `digital/brief/spec.md` and `analog/brief/spec.md` - the brief each
  nested run starts from, naming its side's crossing signals exactly as
  `interface.yaml` does.

`corpus/msde/sensor_counted/` shows the shape.

## The gate

`split` fails on any `signal_missing_from_spec`, `signal_not_declared` or
`<field>_mismatch` finding, on `pad_in_signals` (a signal that is a
`ua_pins` key, an analog `pads` entry or named `ua...`) and on
`pad_missing_from_ua_pins` (an analog `pads` entry `ua_pins` does not
map). The fix is always to make the three files
agree, and the question is which one is wrong: `interface.yaml` is the
authority unless the brief says otherwise.

Once the nested digital workspace exists (`digital/spec/spec.yaml`),
`split` also checks it against the interface. Every a2d signal in
domain `clk_free` that is not one of the digital `clock.domains` must be
`async` under `ports` in `digital/spec/cdc.yaml`. If it is missing, or
given a clock, `split` fails with `cdc_async_undeclared`. A port that file
calls `async` while `interface.yaml` gives it a clocked domain fails
with `cdc_async_contradicts_interface`. Right after creating the digital
workspace, run `eda python engine/scripts/split_cdc.py --workspace <ws>`.
It writes those ports and keeps the rest of the file. It refuses a port the
file already gives a clock: one of the two files is wrong. Without
this, the digital `cdc` gate takes the async input as synchronous to
`clock.primary` (it lists such ports under `assumed_ports`) and passes a
one-flop sample. Until the digital workspace exists, the report has
`cdc.checked: false` and the reason.

## Changing the interface later

The router plans `interface_edit`, which marks `split`, `cosim`,
`top_harden`, `top_drc`, `top_lvs` and `release` stale in this workspace. `docs/design.md` 1.6 says
it also marks both nested runs' `spec_edit`, and the engine cannot reach a
nested `state.json` on its own - so the recipe declares `spec_edit` in
`{ws}/digital` and `{ws}/analog` itself. Run those steps whenever the
nested workspace exists; skipping them leaves a nested pass that reads
fresh against an interface that no longer holds.

After the cascade, re-drive each side through its own router (`resume`)
until it is fresh again, then the top gates. `top_harden` runs as a job
(`jobs.py start`), even where the router lists it as a gate step.

## Freshness

`split` hashes `interface.yaml`, both side specs (`digital_spec.yaml`,
`analog_spec.yaml`), the nested `digital/spec/spec.yaml` and
`digital/spec/cdc.yaml` (`invalidation.yaml` gate_inputs). An edit to any
of them stales a recorded `split` pass.
