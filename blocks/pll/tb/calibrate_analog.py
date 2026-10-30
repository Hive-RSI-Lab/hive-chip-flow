#!/usr/bin/env python
"""calibrate_analog.py - measure the real pll_analog netlist at tt_27c and
write the numbers the ideal cosim model and the linear loop model use.

    eda python blocks/pll/tb/calibrate_analog.py [--corner tt_27c]

Runs ngspice (batch) on analog/netlist/pll_analog.cir with sizing/sizing.yaml,
the same PDK model sections the analog gates load, and writes:
  - tb/pll_analog_cal.json : VCO f(vctrl) table, pump UP/DN current against
    vctrl and per trim code, filter R1/C1/C2 fitted from the cell's own
    vctrl impedance (so the V2I gate and pump off-capacitance are inside C2)
  - tb/pll_analog_ideal.sp : the behavioural stand-in the cosim bench runs
    when analog_kind is "ideal" (generated from the json, not hand-edited)
With --corner ss_pff it writes tb/pll_analog_cal_ss_pff.json alone, which
prescaler_margin.py reads.

Nothing here reads a spec bound; every number is measured.
"""
from __future__ import annotations

import argparse
import cmath
import json
import math
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import pll_tb_common as C  # noqa: E402

CORNERS = {
    # name: (mos, res, mim, temp, vdd)
    "tt_27c": ("typical", "res_typical", "mimcap_typical", 27, 3.3),
    # the skew corner where the VCO is fastest against slow digital
    # (ss transistors, fast passives; prescaler_margin.py reads it)
    "ss_pff": ("ss", "res_ff", "mimcap_ff", 125, 2.97),
}

DUT = ("xdut vco_out pfd_up pfd_dn pll_en cp_trim0 cp_trim1 vctrl bias_ref "
       "vdd vss pll_analog")


def header(corner: str) -> list[str]:
    mos, res, mim, temp, vdd = CORNERS[corner]
    return (["* calibrate_analog.py deck"] + C.model_lines(mos, res, mim) +
            [f".temp {temp}",
             f"vdd_src vdd 0 {vdd}", "vss_src vss 0 0", f"ven pll_en 0 {vdd}"])


def run_deck(lines: list[str], tmp: Path, name: str) -> str:
    deck = tmp / f"{name}.cir"
    deck.write_text("\n".join(lines) + "\n.end\n", encoding="utf-8")
    proc = subprocess.run(C.ngspice_cmd() + [str(deck)], capture_output=True,
                          text=True, cwd=tmp, timeout=1800)
    return proc.stdout + proc.stderr


def vco_point(corner: str, vc: float, tmp: Path) -> float:
    """Mean frequency of 5 periods at a forced vctrl (0 if stalled). The
    1 fC kick at 1 ns is the vco bench's own start-up kick."""
    slow = vc < 0.95
    stop, step, settle = ("4u", "100p", "800n") if slow else ("300n", "20p", "40n")
    vdd = CORNERS[corner][4]
    lines = header(corner) + [
        "vup pfd_up 0 0", "vdn pfd_dn 0 0", "vt0 cp_trim0 0 0",
        "vt1 cp_trim1 0 0", f"vvc vctrl 0 {vc}", "cload vco_out 0 15f", DUT,
        "ikick 0 xdut.r1 pulse(0 1u 1n 50p 50p 1n 1)",
        ".control", "set noaskquit", f"tran {step} {stop} 0 {step}",
        f"meas tran ta when v(vco_out)={vdd / 2} rise=2 td={settle}",
        f"meas tran tb when v(vco_out)={vdd / 2} rise=7 td={settle}",
        "let f = 5/(tb-ta)", 'echo "FREQ $&f"', ".endc"]
    out = run_deck(lines, tmp, f"vco_{vc:.2f}")
    for ln in out.splitlines():
        if ln.startswith("FREQ "):
            try:
                return float(ln.split()[1])
            except (IndexError, ValueError):
                return 0.0
    return 0.0


def pump(corner: str, tmp: Path) -> dict:
    vdd = CORNERS[corner][4]
    lines = header(corner) + [
        "vup pfd_up 0 0", "vdn pfd_dn 0 0", "vt0 cp_trim0 0 0",
        "vt1 cp_trim1 0 0", f"vvc vctrl 0 {vdd / 2}", "cload vco_out 0 15f",
        DUT, ".control", "set noaskquit",
        f"alter vup dc = {vdd}", f"dc vvc 0 {vdd} 0.05",
        "wrdata iup.txt i(vvc)",
        "alter vup dc = 0", f"alter vdn dc = {vdd}", f"dc vvc 0 {vdd} 0.05",
        "wrdata idn.txt i(vvc)", "alter vdn dc = 0",
        f"dc vvc 0 {vdd} 0.05", "wrdata ioff.txt i(vvc)",
        f"alter vup dc = {vdd}"]
    for code, (t0, t1) in {"00": (0, 0), "01": (vdd, 0), "10": (0, vdd),
                           "11": (vdd, vdd)}.items():
        lines += [f"alter vt0 dc = {t0}", f"alter vt1 dc = {t1}", "op",
                  "let x = i(vvc)", f'echo "ICP{code} $&x"']
    lines += [".endc"]
    out = run_deck(lines, tmp, "pump")

    def table(fname, sign):
        rows = []
        for ln in (tmp / fname).read_text().splitlines():
            parts = ln.split()
            if len(parts) >= 2:
                rows.append([round(float(parts[0]), 4),
                             sign * float(parts[1])])
        return rows

    trims = {}
    for ln in out.splitlines():
        if ln.startswith("ICP"):
            trims[ln[3:5]] = float(ln.split()[1])
    return {"up_a_vs_vctrl": table("iup.txt", 1.0),
            "dn_a_vs_vctrl": table("idn.txt", -1.0),
            "off_a_vs_vctrl": table("ioff.txt", 1.0),
            "up_a_by_trim_mid": trims}


def filt(corner: str, tmp: Path) -> dict:
    """Small-signal impedance at vctrl, pump off, VCO running point vdd/2,
    biased through a 1 GH feed (open above 1 kHz), as the ol bench does."""
    vdd = CORNERS[corner][4]
    freqs = [1e3, 1e4, 3e5, 1e6, 3e6, 1e8]
    lines = header(corner) + [
        "vup pfd_up 0 0", "vdn pfd_dn 0 0", "vt0 cp_trim0 0 0",
        "vt1 cp_trim1 0 0", f"vvc vfeed 0 {vdd / 2}", "lfeed vfeed vctrl 1e9",
        "iac 0 vctrl dc 0 ac 1", "cload vco_out 0 15f", DUT,
        ".control", "set noaskquit", "ac dec 50 100 1e9",
        "wrdata zac.txt v(vctrl)", ".endc"]
    run_deck(lines, tmp, "filt")
    rows = []
    for ln in (tmp / "zac.txt").read_text().splitlines():
        parts = ln.split()
        if len(parts) >= 3:
            rows.append((float(parts[0]), complex(float(parts[1]),
                                                  float(parts[2]))))

    def z_at(f):
        return min(rows, key=lambda r: abs(math.log(r[0] / f)))

    # Ctot from the 1 kHz point, C2 from 100 MHz, R from a 1-D fit of the
    # series R1-C1 || C2 model to the whole 10 kHz..30 MHz span.
    f_lo, z_lo = z_at(1e3)
    ctot = 1 / (2 * math.pi * f_lo * abs(z_lo.imag))
    f_hi, z_hi = z_at(1e8)
    c2 = 1 / (2 * math.pi * f_hi * abs(z_hi.imag))
    c1 = ctot - c2
    fit = [(f, z) for f, z in rows if 1e4 <= f <= 3e7]

    def model(f, r):
        s = 2j * math.pi * f
        zc1 = r + 1 / (s * c1)
        zc2 = 1 / (s * c2)
        return zc1 * zc2 / (zc1 + zc2)

    def err(r):
        return sum(abs(cmath.log(model(f, r) / z)) ** 2 for f, z in fit)

    lo, hi = 1e3, 1e6
    for _ in range(200):                       # golden-section on log R
        a = lo * (hi / lo) ** 0.382
        b = lo * (hi / lo) ** 0.618
        if err(a) < err(b):
            hi = b
        else:
            lo = a
    r1 = math.sqrt(lo * hi)
    return {"r1_ohm": r1, "c1_f": c1, "c2_f": c2, "ctot_f": ctot,
            "fit_rms_log_err": math.sqrt(err(r1) / len(fit)),
            "z_points": [[f, abs(z_at(f)[1]),
                          math.degrees(cmath.phase(z_at(f)[1]))] for f in freqs]}


def interp(table, x):
    for (x0, y0), (x1, y1) in zip(table, table[1:]):
        if x0 <= x <= x1:
            return y0 + (y1 - y0) * (x - x0) / (x1 - x0)
    return table[0][1] if x < table[0][0] else table[-1][1]


def write_ideal(cal: dict, path: Path) -> None:
    """The behavioural stand-in: pump = two switched current sources with
    the measured I(vctrl) tables and trim ratios, filter = measured R1/C1/C2,
    VCO = a phase integrator driven by the measured f(vctrl) table. Same pin
    order as the transistor wrapper."""
    vco = cal["vco"]["f_hz_vs_vctrl"]
    up = cal["pump"]["up_a_vs_vctrl"]
    dn = cal["pump"]["dn_a_vs_vctrl"]
    trim = cal["pump"]["up_a_by_trim_mid"]
    t01 = trim["01"] / trim["00"] - 1
    t10 = trim["10"] / trim["00"] - 1
    t11 = trim["11"] / trim["00"] - 1 - t01 - t10
    flt = cal["filter"]

    def pwl(table, scale=1.0):
        return ", ".join(f"{x:.4g}, {y * scale:.6g}" for x, y in table)

    vdd = cal["vdd"]
    vth = vdd / 2
    txt = f"""* pll_analog_ideal.sp - GENERATED by calibrate_analog.py from
* pll_analog_cal.json ({cal['corner']}, measured on analog/netlist/pll_analog.cir
* with analog/sizing/sizing.yaml). Do not hand-edit; re-run the script.
*
* Behavioural stand-in for the transistor cell inside the cosim bridge:
*  - pump: UP sources Iup(vctrl), DN sinks Idn(vctrl) (measured DC tables,
*    code 00, so the pump's compliance near the rails is the cell's own),
*    scaled by the measured trim ratios; off when pll_en is low
*  - filter: R1 = {flt['r1_ohm']:.4g} ohm, C1 = {flt['c1_f']:.4g} F,
*    C2 = {flt['c2_f']:.4g} F, fitted to the cell's own vctrl impedance
*  - VCO: phase integrator (1 F cap, 1 V = 1 cycle) driven by the measured
*    f(vctrl) table, 0 Hz with pll_en low; vco_out = vdd/2*(1+tanh(8 sin))
*    (edges ~2 % of a period)
* Pin order is the cocotbext-ams wrapper's: digital inputs, then outputs,
* then vdd vss. vctrl and the phase node stay internal (x1.vctrl, x1.ph).
.subckt pll_analog_ideal pfd_up pfd_dn pll_en cp_trim0 cp_trim1 vco_out vdd vss
* input conditioning: 20 ps RC so the bridge's stepped sources stay smooth
rin_up pfd_up up_f 1k
cin_up up_f 0 20f
rin_dn pfd_dn dn_f 1k
cin_dn dn_f 0 20f
.param k01={t01:.6g} k10={t10:.6g} k11={t11:.6g}
bsc  ksc 0 v = 1 + k01*(v(cp_trim0) > {vth}) + k10*(v(cp_trim1) > {vth}) + k11*(v(cp_trim0) > {vth})*(v(cp_trim1) > {vth})
ben  en  0 v = v(pll_en) > {vth} ? 1 : 0
bup 0 vctrl i = v(en)*v(ksc)*min(max((v(up_f)-{0.1 * vdd})/{0.8 * vdd}, 0), 1)*pwl(v(vctrl), {pwl(up)})
bdn vctrl 0 i = v(en)*v(ksc)*min(max((v(dn_f)-{0.1 * vdd})/{0.8 * vdd}, 0), 1)*pwl(v(vctrl), {pwl(dn)})
r1 vctrl vfilt {flt['r1_ohm']:.6g}
c1 vfilt 0 {flt['c1_f']:.6g}
c2 vctrl 0 {flt['c2_f']:.6g}
* VCO phase (cycles) on a 1 F cap; gmin keeps it from floating at DC
bph 0 ph i = v(en)*pwl(v(vctrl), {pwl(vco)})
cph ph 0 1
bout vco_out 0 v = {vdd / 2}*(1 + tanh(8*sin(6.283185307179586*v(ph))))
.ends pll_analog_ideal
"""
    path.write_text(txt, encoding="ascii")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--corner", default="tt_27c", choices=sorted(CORNERS))
    ap.add_argument("--jobs", type=int, default=4)
    args = ap.parse_args(argv)
    vdd = CORNERS[args.corner][4]
    grid = ([round(0.1 * k, 2) for k in range(31) if 0.1 * k < vdd - 0.01] +
            [x for x in (3.1, 3.2) if x < vdd] + [vdd])
    with tempfile.TemporaryDirectory(prefix="pllcal_") as td:
        tmp = Path(td)
        with ThreadPoolExecutor(args.jobs) as ex:
            fut_p = ex.submit(pump, args.corner, tmp)
            fut_f = ex.submit(filt, args.corner, tmp)
            subs = {}
            for vc in grid:
                sub = tmp / f"v{vc:.2f}"
                sub.mkdir()
                subs[vc] = ex.submit(vco_point, args.corner, vc, sub)
            vco = [[vc, subs[vc].result()] for vc in grid]
            pmp = fut_p.result()
            flt = fut_f.result()
    # A point the window could not resolve (a stalled ring) reads 0 Hz and
    # stays in the table as measured - the floor current (MN_FLR) keeps the
    # ring running down to vctrl = 0 at tt, so none is expected.
    vco = [[vc, f] for vc, f in vco]
    f100 = 100e6
    v100 = next(x0 + (x1 - x0) * (f100 - y0) / (y1 - y0)
                for (x0, y0), (x1, y1) in zip(vco, vco[1:]) if y0 <= f100 <= y1)
    kvco = (interp(vco, v100 + 0.1) - interp(vco, v100 - 0.1)) / 0.2
    cal = {
        "corner": args.corner, "vdd": vdd,
        "source": "analog/netlist/pll_analog.cir + analog/sizing/sizing.yaml",
        "vco": {"f_hz_vs_vctrl": vco, "vctrl_at_100mhz_v": v100,
                "kvco_at_100mhz_hz_per_v": kvco},
        "pump": pmp,
        "icp_up_mid_a": interp(pmp["up_a_vs_vctrl"], vdd / 2),
        "icp_dn_mid_a": interp(pmp["dn_a_vs_vctrl"], vdd / 2),
        "filter": flt,
    }
    if args.corner == "tt_27c":
        C.CAL_JSON.write_text(json.dumps(cal, indent=1), encoding="utf-8")
        write_ideal(cal, C.HERE / "pll_analog_ideal.sp")
    else:
        # another corner only feeds prescaler_margin.py; the cosim model
        # stays the tt one
        (C.HERE / f"pll_analog_cal_{args.corner}.json").write_text(
            json.dumps(cal, indent=1), encoding="utf-8")
    print(json.dumps({k: cal[k] for k in ("icp_up_mid_a", "icp_dn_mid_a")} |
                     {"vctrl_at_100mhz_v": v100, "kvco_hz_per_v": kvco,
                      "r1_ohm": flt["r1_ohm"], "c1_f": flt["c1_f"],
                      "c2_f": flt["c2_f"], "fit": flt["fit_rms_log_err"]},
                     indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
