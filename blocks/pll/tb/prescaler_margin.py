#!/usr/bin/env python
"""prescaler_margin.py - how fast the /2 prescaler stage has to be at a
corner, and whether the loop gets back from a VCO it cannot divide.

    eda python blocks/pll/tb/prescaler_margin.py [--cal tb/pll_analog_cal_ss_pff.json]

A behavioural loop in time steps, built from a calibrate_analog.py json
(VCO f(vctrl), pump I(vctrl), filter R1/C1/C2, all measured on the real
netlist at that corner): tri-state PFD, the /8 prescaler and /N divider,
the K = 16 lock detector and the digital's recovery (REQ-RECOVER: W = 1024
unlocked reference cycles, then M = 128 cycles of DN). The first prescaler
stage toggles on a VCO edge only if t_lim has passed since its last
toggle, the way a toggle flop whose loop is slower than the VCO drops
edges. Prints, for each 200 MHz point of the lock table (and a slower one),
the highest VCO frequency while acquiring from vctrl = 0, whether it locks
with the given t_lim, and whether it locks from vctrl at the rail with and
without the recovery. Not a gate; the numbers back REQ-TIM-VCO's ss bound.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
POINTS = [(6.25e6, 4), (5e6, 5), (12.5e6, 2), (6.25e6, 3)]  # f_ref, N
W, M, K = 1024, 128, 16


def run(cal: dict, f_ref: float, n: int, t_lim_ns: float, v0: float,
        recover: bool, t_end: float, dt: float = 0.05e-9) -> dict:
    vdd = cal["vdd"]
    fv = np.array(cal["vco"]["f_hz_vs_vctrl"])
    up = np.array(cal["pump"]["up_a_vs_vctrl"])
    dn = np.array(cal["pump"]["dn_a_vs_vctrl"])
    r1, c1, c2 = (cal["filter"][k] for k in ("r1_ohm", "c1_f", "c2_f"))
    t_ref, t_lim = 1 / f_ref, t_lim_ns * 1e-9
    t, vc, v1, ph = 0.0, v0, v0, 0.0
    upq = dnq = 0
    last_tog, toggles = -1.0, 0
    next_ref, sample_at = t_ref, None
    wide, lock_cnt, lock, wd, rec = 1, 0, 0, 0, 0
    f_peak, lock_t, recoveries = 0.0, None, 0
    while t < t_end:
        f = float(np.interp(vc, fv[:, 0], fv[:, 1]))
        if t > 0.5e-6:
            f_peak = max(f_peak, f)
        ph += f * dt
        if ph >= 1.0:                       # a VCO rising edge
            ph -= 1.0
            if t - last_tog >= t_lim:       # stage 0 keeps up: it toggles
                last_tog, toggles = t, toggles + 1
                if toggles % (8 * n) == 0:  # /8 then /N: a clk_fb edge
                    dnq = 1
        if t >= next_ref:                   # a reference edge
            next_ref += t_ref
            upq = 1
            lock_cnt = 0 if (wide or rec) else min(lock_cnt + 1, K)
            new_lock = int(lock_cnt == K)
            if new_lock and lock_t is None:
                lock_t = t
            if recover:
                if lock:
                    wd, rec = 0, 0
                elif wd == (M - 1 if rec else W - 1):
                    wd, rec = 0, 1 - rec
                    recoveries += rec
                else:
                    wd += 1
            lock = new_lock
            sample_at = t + t_ref / 2
        if sample_at is not None and t >= sample_at:
            wide, sample_at = int(upq or dnq or rec), None
        if upq and dnq:
            upq = dnq = 0
        i = ((float(np.interp(vc, up[:, 0], up[:, 1])) if (upq and not rec) else 0.0)
             - (float(np.interp(vc, dn[:, 0], dn[:, 1])) if (dnq or rec) else 0.0))
        ir = (vc - v1) / r1
        vc = min(max(vc + (i - ir) / c2 * dt, 0.0), vdd)
        v1 += ir / c1 * dt
        t += dt
    return {"f_peak_mhz": round(f_peak / 1e6, 1),
            "lock_us": None if lock_t is None else round(lock_t * 1e6, 2),
            "recoveries": recoveries,
            "f_end_mhz": round(float(np.interp(vc, fv[:, 0], fv[:, 1])) / 1e6, 1)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cal", default=str(HERE / "pll_analog_cal_ss_pff.json"))
    ap.add_argument("--t-lim-ns", type=float, default=3.65,
                    help="stage 0's toggle loop (REQ-TIM-VCO's ss bound)")
    args = ap.parse_args(argv)
    cal = json.loads(Path(args.cal).read_text(encoding="utf-8"))
    out = {"corner": cal["corner"], "t_lim_ns": args.t_lim_ns, "points": []}
    for f_ref, n in POINTS:
        out["points"].append({
            "f_ref_mhz": f_ref / 1e6, "n": n, "f_vco_mhz": f_ref * 8 * n / 1e6,
            "ideal_from_0v": run(cal, f_ref, n, 0.0, 0.0, False, 30e-6),
            "from_0v": run(cal, f_ref, n, args.t_lim_ns, 0.0, False, 30e-6),
            "from_rail": run(cal, f_ref, n, args.t_lim_ns, cal["vdd"], False, 60e-6),
            "from_rail_recover": run(cal, f_ref, n, args.t_lim_ns, cal["vdd"], True,
                                     (W + M + 400) / f_ref),
        })
    print(json.dumps(out, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
