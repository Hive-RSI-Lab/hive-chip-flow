#!/usr/bin/env python
"""gmid.py - size a gf180 3.3 V transistor from its gm/Id table (#102).

    gmid.py size --gmid G --id I --l L [--device nfet_03v3|pfet_03v3]
        [--corner tt|ss|ff] [--vds V] [--vsb V]

Reads the pinned pygmid table (engine/lib/gmidlib.py: made into the cache
on first use, refused unless it hashes to engine/reference/gmid's pin)
and prints, as JSON, the W that carries --id at --gmid and --l, the VGS
it sits at, and its gm, gds, gm/gds, Cgg and fT (gm / 2 pi Cgg). Values
take SPICE suffixes (10u, 1.5m). --vds defaults to VDD/2 = 1.65 V; the
pfet's voltages are magnitudes. A W past the models' widest bin
(100 um) comes back as M parallel devices of W_each_um (the `spice` line
carries m=); `warnings` flags a W narrow enough to be less exact.
Off the swept L and VSB points the W holds to about 1% and the VGS to a
few mV: bias the device by its current (a mirror), not by that VGS.

    gmid.py size --gmid 12 --id 10u --l 1u

Exit 0: sized. 2: refused (an unpinned or mismatched table, a gm/Id the
device can't reach at that L, an L outside the sweep), with a remediation.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))

import gmidlib  # noqa: E402

_SUFFIX = {"f": 1e-15, "p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3,
           "k": 1e3, "meg": 1e6, "g": 1e9}


def si(text: str) -> float:
    m = re.fullmatch(r"\s*([-+]?[0-9.]+(?:e[-+]?\d+)?)(meg|[fpnumkg])?\s*",
                     str(text).lower())
    if not m:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}")
    return float(m.group(1)) * _SUFFIX.get(m.group(2) or "", 1.0)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("size")
    s.add_argument("--gmid", type=si, required=True, help="gm/Id, 1/V")
    s.add_argument("--id", type=si, required=True, help="drain current, A")
    s.add_argument("--l", type=si, required=True, help="length, m (1u)")
    s.add_argument("--device", default="nfet_03v3", choices=gmidlib.DEVICES)
    s.add_argument("--corner", default="tt", choices=tuple(gmidlib.CORNERS))
    s.add_argument("--vds", type=si, default=1.65)
    s.add_argument("--vsb", type=si, default=0.0)
    a = ap.parse_args(argv)
    try:
        t = gmidlib.load(a.device, a.corner)
        r = gmidlib.size(t, a.gmid, abs(a.id), a.l * 1e6, vds=a.vds,
                         vsb=a.vsb)
    except (gmidlib.GmidError, OSError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:600],
                          "remediation": "pick a gm/Id, Id and L the table "
                          "covers; for a table refused on its pin, delete "
                          "it from the cache so it is made again, and run "
                          "engine/scripts/gmid_lut.py --check if that still "
                          "differs"}, indent=2))
        return 2
    spice = f"{a.device} w={r['W_each_um']:.4g}u l={r['L_um']:.4g}u"
    if r["M"] > 1:
        spice += f" m={r['M']}"
    r.update(ok=True, device=a.device, corner=a.corner, spice=spice)
    print(json.dumps(r, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
