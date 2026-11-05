"""Fault: the W/4 macro driver of plant_top_harden_weak_driver.py, with the
digital side reading osc_out through a wire of its own name (#191).

Synthesis keeps one alias for the net on u_analog/osc_out, and with
`osc_async` beside the port it keeps `u_sensor_counted_digital.osc_async`
(tests/fixtures/top_harden_dont_touch/renamed, from this plant's harden): a don't-touch rule guessed from the pin's name misses it, and
repair_design buffered the net until the job ran out of memory. Read from
the synthesis probe, the net is don't-touch whatever its name, the harden
finishes, and top_harden fails max_slew_violation on the weak driver like
the plain W/4 row - not a lost job."""
import importlib.util
import re
from pathlib import Path

RTL = "sensor_counted_digital.v"
_weak = importlib.util.spec_from_file_location(
    "plant_top_harden_weak_driver_for_rename",
    Path(__file__).with_name("plant_top_harden_weak_driver.py"))
weak = importlib.util.module_from_spec(_weak)
_weak.loader.exec_module(weak)


def _rename(ws: Path) -> None:
    rtl = ws / "digital" / "rtl" / RTL
    text = rtl.read_text(encoding="utf-8")
    text, n = re.subn(r"(\n  // osc_en = en)",
                      "\n  wire osc_async = osc_out;\n\\1", text, count=1)
    text, m = re.subn(r"osc_sync0   <= osc_out;", "osc_sync0   <= osc_async;",
                      text)
    if n != 1 or m != 1:
        raise RuntimeError("plant_top_harden_renamed_net.py: the digital RTL "
                           "no longer reads osc_out where expected")
    rtl.write_text(text, encoding="utf-8")


def plant(ws: Path) -> None:
    # faults.macro_lib_first has run macro_lib on the untouched design; the
    # stock harden's pass is top_harden_macro_driver_w4's to prove
    _rename(ws)
    stock = weak._weaken(ws)
    try:
        weak._macro_lib(ws, stock=False)
    finally:
        (sorted((ws / "analog" / "netlist").glob("*.cir"))[0]
         ).write_text(stock, encoding="utf-8")
