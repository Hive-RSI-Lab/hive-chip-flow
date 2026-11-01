# deliverable_untied

`release` staged a file into the submission tree that no gate record holds
(`engine/lib/manifestlib.py`). Nothing signed off what it says, so it does not
ship as it is.

Most often it is a hand-kept file under the block's `tt/`:

- `tt/src/project.v` or `tt/src/<macro>.v` differs in code (comments and
  whitespace aside) from the generated `top/harden/<top>.v` or `top/macros/*.v`.
  Delete the hand-kept copy so the generated one ships, or make it say the same.
- `tt/info.yaml` disagrees with `top/harden/info.yaml` on top_module, tiles or
  analog_pins. Fix `tt/info.yaml`; the harden used the other one.

If the difference is real and a person has reviewed it, add a waiver bound to
the file's bytes to `reports/waivers.json`:
`{"deliverable": "src/project.v", "sha256": "<its sha256>", "reason": "...",
"approved_by": "..."}`. Any later edit to the file voids it.

Never change the check, and never have an agent approve its own waiver.
