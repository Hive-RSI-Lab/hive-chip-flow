#!/usr/bin/env python
"""lctime_shim.py - run lctime 0.0.26 past what engine/lib/charlib.py cannot
correct from outside it. Takes lctime's own arguments.

find_minimum_clock_pulse_width asserts "Signal crosses threshold multiple times"
on gf180 dffq_1 at time steps 10, 2 and 1 ps. lctime only uses the answer as a
clock width long enough to sample, so it is given CHARLIB_CLOCK_PULSE_NS instead.
That number is an input, not a measurement: charlib measures min_pulse_width
itself (issue #172) and writes its own.

characterize_flip_flop_setup_hold is skipped too: its setup and hold are
"dependent" times, 1.9-4.2x the PDK's on dffq_1, and charlib replaces all four
tables with its own bisection, so the hours lctime spends on them are wasted.
It returns zeros of the shape lctime expects; charlib refuses a cell whose
constraint tables it did not overwrite. Its cell_rise/cell_fall are not used by
lctime's caller (clock-to-Q comes from measure_clock_to_output_delay).
"""
from __future__ import annotations

import os
import sys

import numpy as np
from lctime.characterization import timing_sequential as seq


def _no_setup_hold(*_a, related_pin_transition, constrained_pin_transition, **_k):
    related = np.asarray(related_pin_transition, dtype=float)
    constrained = np.asarray(constrained_pin_transition, dtype=float)
    zeros = np.zeros((len(related), len(constrained)))
    return {"related_pin_transition": related,
            "constrained_pin_transition": constrained,
            **{f"{k}_{e}_constraint": zeros.copy()
               for k in ("setup", "hold") for e in ("rise", "fall")},
            "cell_rise": zeros.copy(), "cell_fall": zeros.copy()}


def main() -> None:
    pulse_s = float(os.environ.get("CHARLIB_CLOCK_PULSE_NS", "2")) * 1e-9
    seq.find_minimum_clock_pulse_width = lambda *a, **k: (pulse_s, pulse_s / 2)
    seq.characterize_flip_flop_setup_hold = _no_setup_hold
    from lctime.characterization.main_lctime import main as lctime_main
    sys.argv[0] = "lctime"
    lctime_main()


if __name__ == "__main__":
    main()
