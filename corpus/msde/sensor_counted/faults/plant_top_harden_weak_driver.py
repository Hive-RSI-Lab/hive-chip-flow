"""Fault: a macro output driver too weak for its own liberty limits
(gates.yaml's msde top_harden row, the signoff STA's max slew check).

Declares macro_timing for the analog side (osc_out's limits are sized so
the stock output buffer meets them at every corner), runs macro_lib and a
real top harden on the untouched design, and requires it to pass with the
macro's per-corner liberty loaded at every STA corner - so the lib view,
not a black box, is what the harden timed. Then it shrinks osc_out's
output buffer to a quarter of its width (W/4) and re-runs macro_lib: the
new libs carry the weak driver's slower edges against the same
max_transition, and top_harden must fail with max_slew_violation on
osc_out."""
from pathlib import Path
import re

MACRO = "sensor_counted_analog"
PIN = "osc_out"

# osc_out limits: the stock buffer's worst edge at max_capacitance_pf is
# under max_transition_ns at every corner (macro_lib warns
# macro_limit_inconsistent if not); the W/4 buffer's is over it at ss.
MACRO_TIMING = {
    "pins": {"osc_en": {"max_transition_ns": 2.0},
             PIN: {"max_transition_ns": 3.2, "max_capacitance_pf": 0.012}},
    "static": {"osc_en": "vdd"},
    "arcs": [{"to": PIN, "source": True, "enable": "osc_en",
              "settle_ns": 1500, "window_ns": 2500, "step_ns": 0.02}],
}

STOCK = {"xmpb": "w=2e-6", "xmnb": "w=1e-6"}
WEAK = {"xmpb": "w=5e-7", "xmnb": "w=2.5e-7"}


def _declare_timing(ws: Path) -> None:
    import yaml
    spec_p = ws / "analog" / "spec" / "spec.yaml"
    spec = yaml.safe_load(spec_p.read_text(encoding="utf-8"))
    spec["macro_timing"] = MACRO_TIMING
    spec_p.write_text(yaml.safe_dump(spec, sort_keys=False), encoding="utf-8")


def _macro_lib(ws: Path, stock: bool) -> None:
    """Characterize the macro. The stock driver must meet its own limits
    (no warning at all); the weak one is expected to warn
    macro_limit_inconsistent and nothing else."""
    import check_macro_lib
    payload, _out = check_macro_lib.run(["--workspace", str(ws)])
    other = [v for v in payload.get("violations") or []
             if stock or v.get("kind") != "macro_limit_inconsistent"]
    if other:
        raise RuntimeError(f"plant_top_harden_weak_driver.py: macro_lib did "
                           f"not pass: {payload.get('violations')}")


def _lib_view_loaded(ws: Path) -> None:
    """Every signoff STA corner read this macro's liberty and nothing was
    black-boxed: else the pass above timed no macro pins at all."""
    run = ws / "top" / "harden" / "runs" / "run"
    logs = sorted(run.glob("*-openroad-stapostpnr/*/sta.log"))
    if not logs:
        raise RuntimeError("plant_top_harden_weak_driver.py: no signoff STA "
                           f"logs under {run}")
    for log in logs:
        text = log.read_text(encoding="utf-8", errors="replace")
        if f"{MACRO}__" not in text:
            raise RuntimeError(f"plant_top_harden_weak_driver.py: {log} did "
                               f"not read a {MACRO} liberty")
    for log in run.rglob("*.log"):
        if "black-box" in log.read_text(encoding="utf-8", errors="replace"):
            raise RuntimeError(f"plant_top_harden_weak_driver.py: {log} "
                               f"black-boxed a cell")


def _weaken(ws: Path) -> None:
    nets = sorted((ws / "analog" / "netlist").glob("*.cir"))
    if len(nets) != 1:
        raise RuntimeError(f"plant_top_harden_weak_driver.py: expected one "
                           f"analog netlist, found {nets}")
    text = nets[0].read_text(encoding="utf-8")
    for dev, w in WEAK.items():
        pat = re.compile(rf"^({dev} {PIN} .*?){STOCK[dev]}\b", re.M)
        text, n = pat.subn(rf"\g<1>{w}", text)
        if n != 1:
            raise RuntimeError(f"plant_top_harden_weak_driver.py: no "
                               f"{dev} {PIN} {STOCK[dev]} line to weaken")
    nets[0].write_text(text, encoding="utf-8")


def plant(ws: Path) -> None:
    import check_top_harden
    _declare_timing(ws)
    _macro_lib(ws, stock=True)
    payload, _out = check_top_harden.run(["--workspace", str(ws)])
    if payload.get("status") != "pass":
        raise RuntimeError(f"plant_top_harden_weak_driver.py: top_harden did "
                           f"not pass with the stock driver: "
                           f"{payload.get('violations')}")
    _lib_view_loaded(ws)
    _weaken(ws)
    _macro_lib(ws, stock=False)
