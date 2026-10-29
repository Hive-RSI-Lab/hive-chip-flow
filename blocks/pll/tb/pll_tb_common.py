"""Shared helpers for the top-level PLL benches (cosim, calibration, linear
model). Not a test module. Paths are resolved from this file, so the benches
run from any working directory."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent          # blocks/pll/tb
WS = HERE.parent                                # blocks/pll
ANALOG = WS / "analog"
NETLIST = ANALOG / "netlist" / "pll_analog.cir"
SIZING = ANALOG / "sizing" / "sizing.yaml"
CAL_JSON = HERE / "pll_analog_cal.json"
BENCH_JSON = HERE / "cosim_bench.json"

VDD = 3.3
F_REF_HZ = 6.25e6          # nominal reference (brief section 2)
N_SEL = 2                  # N = 2, P*N = 16 -> 100 MHz
P = 8


def toolchain_root() -> Path:
    """The eda toolchain root: from LIBNGSPICE_PATH (check_cosim.py sets
    it), else EDA_TOOLCHAIN, else `eda --print-toolchain-root`."""
    lib = os.environ.get("LIBNGSPICE_PATH")
    if lib:
        # <T>/foss/tools/ngspice/lib/libngspice.so.0
        return Path(lib).resolve().parents[4]
    if os.environ.get("EDA_TOOLCHAIN"):
        return Path(os.environ["EDA_TOOLCHAIN"])
    cfh = Path(os.environ.get("CHIP_FLOW_HOME", "/opt/chip-flow"))
    out = subprocess.run([str(cfh / "bin" / "eda"), "--print-toolchain-root"],
                         capture_output=True, text=True, check=True)
    return Path(out.stdout.strip())


def pdk_ngspice_dir() -> Path:
    root = os.environ.get("PDK_ROOT")
    base = Path(root) if root else toolchain_root() / "foss" / "pdks"
    return base / "gf180mcuD" / "libs.tech" / "ngspice"


def sizing_param_line() -> str:
    """sizing/sizing.yaml rendered as one .param line - the same values the
    analog gates simulate (each entry's `value`)."""
    import yaml
    doc = yaml.safe_load(SIZING.read_text(encoding="utf-8")) or {}
    parts = [f"{k}={v['value']}" for k, v in doc.items()
             if isinstance(v, dict) and "value" in v]
    return ".param " + " ".join(parts)


def model_lines(corner: str = "typical", res: str = "res_typical",
                mim: str = "mimcap_typical") -> list[str]:
    """PDK model includes + the real netlist + its sizing (tt by default)."""
    p = pdk_ngspice_dir()
    return [
        f".include '{p}/design.spice'",
        f".lib '{p}/sm141064.spice' {corner}",
        f".lib '{p}/sm141064.spice' {res}",
        f".lib '{p}/sm141064.spice' {mim}",
        f".include '{NETLIST}'",
        sizing_param_line(),
    ]


def ngspice_cmd() -> list[str]:
    cfh = Path(os.environ.get("CHIP_FLOW_HOME", "/opt/chip-flow"))
    eda = cfh / "bin" / "eda"
    if eda.is_file():
        return [str(eda), "ngspice", "-b"]
    return [shutil.which("ngspice") or "ngspice", "-b"]


def load_cal() -> dict:
    return json.loads(CAL_JSON.read_text(encoding="utf-8"))


def bench_meta() -> dict:
    return json.loads(BENCH_JSON.read_text(encoding="utf-8"))
