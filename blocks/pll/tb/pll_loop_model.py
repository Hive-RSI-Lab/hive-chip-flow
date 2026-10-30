#!/usr/bin/env python
"""pll_loop_model.py - linear (continuous-time) model of the PLL loop.

    eda python blocks/pll/tb/pll_loop_model.py [--pn 16] [--pad-pf 0]

Layer 1 of the GOALS.md verification stack. Uses the numbers
calibrate_analog.py measured on the real netlist at tt_27c
(tb/pll_analog_cal.json): mean pump current at vctrl = vdd/2, the local
Kvco at the 100 MHz point, and the R1/C1/C2 fitted to the cell's own vctrl
impedance. Open loop:

    H(s) = Icp/(2 pi) * Z(s) * 2 pi Kvco / s / (P*N),
    Z(s) = (R1 + 1/(s C1)) || 1/(s (C2 + Cpad))

Prints and writes reports/loop_model.json: crossover fc, phase margin, the
second-order wn / zeta (C2 ignored), the zero and pole frequencies, the
small-signal settling estimate 4/(zeta wn), and the Gardner stability
limit check (wc < f_ref * pi / ... reported as fc / f_ref). Exit 1 if the
phase margin is under the analog spec's 45 deg (ol_pm_pn16_nopad_deg, tt)
at P*N = 16 with no pad - the same requirement, from the same cell, by an
independent route (the analog gate measures it with ngspice .ac).
"""
from __future__ import annotations

import argparse
import cmath
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pll_tb_common as C  # noqa: E402

PM_MIN_DEG = 45.0  # analog spec ol_pm_pn16_nopad_deg, tt_27c


def analyse(cal: dict, pn: int, pad_f: float) -> dict:
    icp = (cal["icp_up_mid_a"] + cal["icp_dn_mid_a"]) / 2
    kvco = cal["vco"]["kvco_at_100mhz_hz_per_v"]
    r1 = cal["filter"]["r1_ohm"]
    c1 = cal["filter"]["c1_f"]
    c2 = cal["filter"]["c2_f"] + pad_f

    def h(f):
        s = 2j * math.pi * f
        za = r1 + 1 / (s * c1)
        zb = 1 / (s * c2)
        z = za * zb / (za + zb)
        return icp / (2 * math.pi) * z * 2 * math.pi * kvco / s / pn

    lo, hi = 1e3, 1e9                      # |H| falls monotonically here
    for _ in range(200):
        mid = math.sqrt(lo * hi)
        if abs(h(mid)) > 1:
            lo = mid
        else:
            hi = mid
    fc = math.sqrt(lo * hi)
    pm = 180 + math.degrees(cmath.phase(h(fc)))
    wn = math.sqrt(icp * kvco * 2 * math.pi / (2 * math.pi * pn * c1))
    zeta = r1 * c1 * wn / 2
    return {
        "p_times_n": pn, "pad_f": pad_f,
        "icp_a": icp, "kvco_hz_per_v": kvco, "r1_ohm": r1, "c1_f": c1,
        "c2_f": c2,
        "crossover_hz": fc, "phase_margin_deg": pm,
        "fn_hz": wn / (2 * math.pi), "zeta": zeta,
        "zero_hz": 1 / (2 * math.pi * r1 * c1),
        "pole_hz": (c1 + c2) / (2 * math.pi * r1 * c1 * c2),
        "settle_4_over_zeta_wn_us": 4 / (zeta * wn) * 1e6,
        "crossover_over_fref": fc / C.F_REF_HZ,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pn", type=int, default=C.P * C.N_SEL)
    ap.add_argument("--pad-pf", type=float, default=0.0)
    args = ap.parse_args(argv)
    cal = C.load_cal()
    main_pt = analyse(cal, args.pn, args.pad_pf * 1e-12)
    sweep = {f"pn{pn}": analyse(cal, pn, 0.0) for pn in (8, 16, 24, 32, 40)}
    sweep["pn16_pad5p"] = analyse(cal, 16, 5e-12)
    out = {"corner": cal["corner"], "point": main_pt,
           "sweep": {k: {kk: round(v[kk], 4) for kk in
                         ("crossover_hz", "phase_margin_deg", "fn_hz", "zeta",
                          "settle_4_over_zeta_wn_us")}
                     for k, v in sweep.items()},
           "pm_min_deg": PM_MIN_DEG}
    rep = C.WS / "reports"
    rep.mkdir(exist_ok=True)
    (rep / "loop_model.json").write_text(json.dumps(out, indent=1),
                                         encoding="utf-8")
    p = main_pt
    print(f"P*N={p['p_times_n']}  Icp={p['icp_a'] * 1e6:.2f} uA  "
          f"Kvco={p['kvco_hz_per_v'] / 1e6:.1f} MHz/V  R1={p['r1_ohm'] / 1e3:.2f} k  "
          f"C1={p['c1_f'] * 1e12:.2f} p  C2={p['c2_f'] * 1e12:.2f} p")
    print(f"fc={p['crossover_hz'] / 1e3:.0f} kHz (f_ref/{1 / p['crossover_over_fref']:.1f})  "
          f"PM={p['phase_margin_deg']:.1f} deg  fn={p['fn_hz'] / 1e3:.0f} kHz  "
          f"zeta={p['zeta']:.2f}  4/(zeta wn)={p['settle_4_over_zeta_wn_us']:.2f} us")
    for k, v in out["sweep"].items():
        print(f"  {k:11s} fc={v['crossover_hz'] / 1e3:7.0f} kHz  "
              f"PM={v['phase_margin_deg']:5.1f} deg  zeta={v['zeta']:.2f}")
    ok = (args.pn != 16 or args.pad_pf != 0 or
          p["phase_margin_deg"] >= PM_MIN_DEG)
    print("PASS" if ok else f"FAIL: PM < {PM_MIN_DEG} deg")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
