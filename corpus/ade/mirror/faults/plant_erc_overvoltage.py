"""plant_erc_overvoltage.py - ade/mirror `erc` fault: iout, xmout's drain,
is put on a 6 V net (spec.yaml erc_nets iout 0..6 V, as when the mirror's
output sinks from a 6 V rail). xmout is a 3.3 V device (nfet_03v3, rated
3.63 V), so CVC sees its gate-to-drain and drain-to-source voltages over
the rating, and the erc gate names xmout.
"""
from __future__ import annotations

from pathlib import Path

OLD = "  iref_node: {min: 0, max: 0.6}\n"
NEW = OLD + "  iout: {min: 0, max: 6.0}\n"


def plant(ws) -> None:
    path = Path(ws) / "spec" / "spec.yaml"
    text = path.read_text(encoding="utf-8")
    if OLD not in text:
        raise RuntimeError(f"{path}: erc_nets iref_node line not found as "
                           "expected - spec.yaml's shape changed")
    path.write_text(text.replace(OLD, NEW), encoding="utf-8")
