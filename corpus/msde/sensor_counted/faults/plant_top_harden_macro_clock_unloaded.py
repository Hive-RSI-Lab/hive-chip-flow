"""Fault: a macro clock output with no net CTS can name (#191).

The setup of plant_top_harden_macro_clock.py, with nothing in the top left
on the clock: no divider, and the synchroniser no longer samples it. Whatever
synthesis leaves on the pin, no tree can be walked from it, so top_harden
must fail with macro_clock_net_unresolved rather than call an untraced clock
proven. The netlist's own renamed net can't be planted from RTL: synthesis
puts a buffer between the clock and an output port it drives, which is
macro_clock_buffer_unproven, so tests/test_check_top_harden.py renames the
net in a real run's netlist instead."""
import importlib.util
from pathlib import Path

_clock = importlib.util.spec_from_file_location(
    "plant_top_harden_macro_clock_for_unloaded",
    Path(__file__).with_name("plant_top_harden_macro_clock.py"))
clock = importlib.util.module_from_spec(_clock)
_clock.loader.exec_module(clock)


def plant(ws: Path) -> None:
    clock.declare(ws)
    clock.edit_rtl(ws, ("osc_sync0   <= osc_out;", "osc_sync0   <= 1'b1;"))
