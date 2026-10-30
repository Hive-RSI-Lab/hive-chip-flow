#!/usr/bin/env python
"""run_layers.py - run the three loop-verification layers in one command.

    $CHIP_FLOW_HOME/bin/eda python blocks/pll/tb/run_layers.py [--skip 1,2,3]

  1. linear loop model      pll_loop_model.py (fc, phase margin, zeta) from
                            the calibration of the real netlist
  2. RTL + calibrated model the cosim bench with the behavioural stand-in
                            (pll_analog_ideal.sp), reports/cosim_measures_ideal.json
  3. RTL + transistor cell  the msde `cosim` gate itself (check_cosim.py) on
                            whatever cosim_bench.json names (the real netlist),
                            reports/cosim_measures.json

Layers 2 and 3 are checked against the same bounds (pll_cosim.bounds.json).
Exit 0 only when every layer that ran passed. Re-run calibrate_analog.py
first if the analog netlist or sizing changed (it regenerates the
calibration json and pll_analog_ideal.sp).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import pll_tb_common as C  # noqa: E402

CFH = Path(os.environ.get("CHIP_FLOW_HOME", "/opt/chip-flow"))
EDA = CFH / "bin" / "eda"
SCRIPTS = CFH / "engine" / "scripts"


def layer1() -> bool:
    return subprocess.run([sys.executable, str(HERE / "pll_loop_model.py")]
                          ).returncode == 0


def layer2() -> bool:
    sys.path.insert(0, str(SCRIPTS))
    sys.path.insert(0, str(CFH / "engine" / "lib"))
    import check_cosim  # noqa: E402
    import cocotblib  # noqa: E402
    check_cosim.ensure_ngspice_env(check_cosim.toolchain_root())
    os.environ["PLL_COSIM_KIND"] = "ideal"
    meta = check_cosim.load_bench_meta(HERE)
    out = C.WS / "reports" / "cosim_measures_ideal.json"
    out.unlink(missing_ok=True)
    build = C.WS / "log" / "cosim_ideal_build"
    shutil.rmtree(build, ignore_errors=True)
    xml = cocotblib.run_cocotb(build, HERE, check_cosim.collect_sources(HERE),
                               meta["top"], cocotblib.test_modules(HERE),
                               C.WS / "log" / "cosim_ideal_results.xml",
                               timeout_s=check_cosim.COCOTB_TIMEOUT_S)
    del os.environ["PLL_COSIM_KIND"]
    results = cocotblib.parse_results_xml(xml)
    log = (build / "sim.log").read_text(errors="replace")
    meas = json.loads(out.read_text()) if out.is_file() else None
    bad = check_cosim.evaluate_measures(meas, meta["bounds"])
    nonconv = check_cosim.scan_nonconvergence(log)
    failed = [n for n, r in results.items() if not r["passed"]]
    for v in bad:
        print("  ", v["kind"], v["msg"])
    if nonconv:
        print("   ngspice non-convergence:", nonconv)
    if failed:
        print("   failed tests:", failed)
    if meas:
        print(f"   lock {meas['lock_time_us']} us, f_fb {meas['fb_freq_hz']} Hz, "
              f"f_vco {meas['vco_freq_hz']} Hz, vctrl p-p "
              f"{meas['vctrl_pp_last_10us_mv']} mV")
    return not (bad or nonconv or failed)


def layer3() -> bool:
    out = C.WS / "log" / "cosim_gate.json"
    rc = subprocess.run([str(EDA), "python", str(SCRIPTS / "check_cosim.py"),
                         "--workspace", str(C.WS), "--out", str(out)]).returncode
    rep = json.loads(out.read_text()) if out.is_file() else {}
    m = rep.get("measures") or {}
    print(f"   status {rep.get('status')}, analog_kind {rep.get('analog_kind')}, "
          f"violations {len(rep.get('violations') or [])}")
    if m:
        print(f"   lock {m.get('lock_time_us')} us, f_fb {m.get('fb_freq_hz')} Hz, "
              f"f_vco {m.get('vco_freq_hz')} Hz, vctrl p-p "
              f"{m.get('vctrl_pp_last_10us_mv')} mV")
    for v in rep.get("violations") or []:
        print("  ", v.get("kind"), v.get("msg"))
    return rc == 0 and rep.get("status") == "pass"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--skip", default="", help="comma list of layers to skip")
    args = ap.parse_args(argv)
    skip = {s.strip() for s in args.skip.split(",") if s.strip()}
    layers = [("1", "linear loop model", layer1),
              ("2", "RTL + calibrated behavioural model (ngspice)", layer2),
              ("3", "RTL + transistor netlist (cosim gate)", layer3)]
    res = {}
    for key, name, fn in layers:
        if key in skip:
            continue
        print(f"== layer {key}: {name}", flush=True)
        res[key] = fn()
        print(f"== layer {key}: {'PASS' if res[key] else 'FAIL'}", flush=True)
    print("SUMMARY", " ".join(f"L{k}={'pass' if v else 'FAIL'}"
                              for k, v in res.items()))
    return 0 if all(res.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
