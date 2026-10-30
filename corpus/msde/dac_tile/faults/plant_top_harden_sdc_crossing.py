"""Fault: a digital-side constraint on a crossing signal the top cannot
carry (gates.yaml's msde top_harden row), the PLL's own miss: the top was
hardened from the digital side's spec alone, so every constraint in its
digital/harden/constraints.sdc (the PLL's vco_out clock on ui_in[3], its
generated clocks and clock groups) was dropped and the top's other clock
domains went unconstrained. top_harden now carries that file into
top/harden/constraints.sdc with each crossing signal's pad bit moved onto
the macro's pin ([get_ports {ui_in[3]}] -> [get_pins {u_analog/vco_out}]),
and refuses one that names such a bit anywhere else, since in the top that
pad reaches nothing.

dac_tile's bmsb leaves the standalone digital tile on uio_out[0] and goes
to the analog macro's pin in the top. The plant gives the digital side a
constraints.sdc that names uio_out[0] outside [get_ports ...]: top_harden
must refuse it, naming the macro pin and top.sdc, before any harden."""
from pathlib import Path

SDC = """\
# bmsb's pad bit, named bare: a form top_harden cannot move onto the macro
set_false_path -to {uio_out[0]}
"""


def plant(ws: Path) -> None:
    import yaml
    spec = yaml.safe_load((ws / "digital" / "spec" / "spec.yaml").read_text(
        encoding="utf-8"))
    if (spec.get("tt_pins") or {}).get("bmsb") != "uio_out[0]":
        raise RuntimeError("plant_top_harden_sdc_crossing.py: expected the "
                           "digital spec's tt_pins to put bmsb on uio_out[0], "
                           f"found {spec.get('tt_pins')}")
    harden = ws / "digital" / "harden"
    harden.mkdir(parents=True, exist_ok=True)
    (harden / "constraints.sdc").write_text(SDC, encoding="utf-8")
