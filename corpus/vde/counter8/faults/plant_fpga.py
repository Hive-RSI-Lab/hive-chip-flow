"""Fault: a combinational loop (gates.yaml's fpga row, #96) - the same
loop as plant_synth.py's: `lp` feeds its own value and `count`'s next
state. The ASIC flow's ABC breaks such a loop and finishes; an FPGA cannot
time it, so the fpga step fails it by name."""
import importlib.util
from pathlib import Path


def plant(ws: Path) -> None:
    src = Path(__file__).with_name("plant_synth.py")
    spec = importlib.util.spec_from_file_location("plant_synth_for_fpga", src)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.plant(ws)
