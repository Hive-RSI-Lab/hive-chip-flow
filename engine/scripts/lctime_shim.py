#!/usr/bin/env python
"""lctime_shim.py - run lctime 0.0.26 past the one defect engine/lib/charlib.py
cannot correct from outside it. Takes lctime's own arguments.

find_minimum_clock_pulse_width asserts "Signal crosses threshold multiple times"
on gf180 dffq_1 at time steps 10, 2 and 1 ps. lctime only uses the answer as a
clock width long enough to sample, so it is given CHARLIB_CLOCK_PULSE_NS instead,
and charlib drops min_pulse_width from what it writes: this number is an input,
not a measurement.
"""
from __future__ import annotations

import os
import sys

from lctime.characterization import timing_sequential as seq


def main() -> None:
    pulse_s = float(os.environ.get("CHARLIB_CLOCK_PULSE_NS", "2")) * 1e-9
    seq.find_minimum_clock_pulse_width = lambda *a, **k: (pulse_s, pulse_s / 2)
    from lctime.characterization.main_lctime import main as lctime_main
    sys.argv[0] = "lctime"
    lctime_main()


if __name__ == "__main__":
    main()
