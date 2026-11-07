# sim_tone_refused (sim_tt, sim_pvt, bench_strength, mc)

A bench uses `{{TONE_FILE}}`, and its record could not be scored through
`convlib.dynamic`. The refusal's kind says which of these happened:
- `bad_input`: there is no `* tone: fs=.. fin=.. nbits=.. n=..` line, the
  file does not hold exactly n samples, or the samples are not evenly spaced
  at 1/fs (or by 1, as an index).
- `no_record`: ngspice wrote no file.
- `not_coherent`: fin does not put a whole number of periods in the record,
  or the record's largest tone is not at fin.
- `bin_not_coprime`: the number of periods shares a factor with n.

**Cheapest fix first:** read the message and the deck under `log/sim/`. Pick
fin with `convlib.coherent_tone(fs, n, fin_target)` and write that exact value
in both the source and the `* tone:` line. Sample once per 1/fs, n times, then
`wrdata {{TONE_FILE}} <vector>` with exactly one vector, with `set numdgt=15`
first.

**Trap:** never fix a `not_coherent` record with a window or by trimming
samples until it passes. A record that is not coherent leaks the tone into
its neighbouring bins, and the ENOB it gives belongs to the leakage, not to
the converter.
