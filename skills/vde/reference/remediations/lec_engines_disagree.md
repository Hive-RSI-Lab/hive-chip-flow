# lec_engines_disagree (lec)

kepler-formal found a difference on the netlist link and eqy proved none. One
of the two tools is wrong, and that is not a pass. Routes to `review`.

Compare `log/lec/kepler/miter_log_*.txt` with `log/lec/synth_vs_final/`.
The other way round (eqy differs, kepler sees none) is expected for a flop whose
type changed: kepler-formal can't see that, and it is not this finding.
