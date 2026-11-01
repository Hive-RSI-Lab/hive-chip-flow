# macro_limit_waived (top_harden)

`harden_signoff` waives a slew or cap violation on a macro pin whose limit the
analog spec's `macro_timing` declares. Both can't hold. Routes to `top_harden`.

**Fix:** if the analog side really tolerates the slower edge, raise the number
in `macro_timing.pins` (from a simulation at that edge) and re-run `macro_lib`.
Otherwise drop the waiver and fix the digital driver.
