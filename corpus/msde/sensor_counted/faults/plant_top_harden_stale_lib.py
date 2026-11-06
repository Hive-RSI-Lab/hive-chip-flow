"""Fault: the macro's netlist changed after the macro_lib gate ran (#177).

faults.macro_lib_first has characterised the untouched design. This shrinks
the last stage of osc_out's output buffer to minimum width
(plant_top_harden_weak_driver.py's change) and leaves macro_lib as it was,
so its libs time the old driver: top_harden must fail macro_lib_stale
before any harden runs, not time the macro on them."""
import importlib.util
from pathlib import Path

_weak = importlib.util.spec_from_file_location(
    "plant_top_harden_weak_driver_for_stale",
    Path(__file__).with_name("plant_top_harden_weak_driver.py"))
weak = importlib.util.module_from_spec(_weak)
_weak.loader.exec_module(weak)


def plant(ws: Path) -> None:
    weak._weaken(ws)
