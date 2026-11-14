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

lctime drives a combinational arc's input with a linear ramp (StepWave). When
the template states a normalized_driver_waveform, charlib passes it in
CHARLIB_DRIVER_JSON (a Terms) and the input edge takes that shape instead
(charlib.driver_edge, issue #322). The ramp lctime is given is ramp_of(slew),
which turns back into the library slew. Sequential arcs keep the linear ramp.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import numpy as np
from lctime.characterization import piece_wise_linear as pwl
from lctime.characterization import timing_combinatorial as comb
from lctime.characterization import timing_sequential as seq

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
import charlib  # noqa: E402


def _no_setup_hold(*_a, related_pin_transition, constrained_pin_transition, **_k):
    related = np.asarray(related_pin_transition, dtype=float)
    constrained = np.asarray(constrained_pin_transition, dtype=float)
    zeros = np.zeros((len(related), len(constrained)))
    return {"related_pin_transition": related,
            "constrained_pin_transition": constrained,
            **{f"{k}_{e}_constraint": zeros.copy()
               for k in ("setup", "hold") for e in ("rise", "fall")},
            "cell_rise": zeros.copy(), "cell_fall": zeros.copy()}


def _terms_of_json(text: str):
    d = json.loads(text)

    def tup(x):
        return tuple(tup(v) for v in x) if isinstance(x, list) else x
    return charlib.Terms(**{k: tup(v) for k, v in d.items()})


def driver_step_wave(terms):
    """A StepWave stand-in for timing_combinatorial: the edge starts at
    start_time (lctime passes thresholds 0 and 1 there) and has the template
    driver's shape for the library slew its ramp stands for."""
    def make(start_time, polarity=True, transition_time=0.0,
             rise_threshold=0.5, fall_threshold=0.5, **_k):
        if (rise_threshold if polarity else 1 - fall_threshold) != 0:
            raise ValueError("driver shim: lctime no longer starts its "
                             "combinational edge at start_time")
        slew = (transition_time * 1e9 * (terms.slew_upper - terms.slew_lower)
                / terms.derate)
        shape = charlib.driver_edge(terms, slew, polarity)
        return pwl.PieceWiseLinear([start_time + t * 1e-9 for t, _ in shape],
                                   [f for _, f in shape])
    return make


def main() -> None:
    driver = os.environ.get("CHARLIB_DRIVER_JSON")
    if driver:
        terms = _terms_of_json(driver)
        if terms.driver:
            comb.StepWave = driver_step_wave(terms)
    pulse_s = float(os.environ.get("CHARLIB_CLOCK_PULSE_NS", "2")) * 1e-9
    seq.find_minimum_clock_pulse_width = lambda *a, **k: (pulse_s, pulse_s / 2)
    seq.characterize_flip_flop_setup_hold = _no_setup_hold
    from lctime.characterization.main_lctime import main as lctime_main
    sys.argv[0] = "lctime"
    lctime_main()


if __name__ == "__main__":
    main()
