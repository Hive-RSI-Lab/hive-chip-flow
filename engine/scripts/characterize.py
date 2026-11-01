#!/usr/bin/env python
"""characterize.py - one cell's liberty, one .lib per corner (engine/lib/charlib.py).

    characterize.py --cell C --out DIR [--extracted X] [--schematic S]
        [--library L] [--template-corner tt_025C_3v30] [--template-lib F]
        [--cell-lib F --reference-cell R] [--nominal-vdd 3.3]
        [--corners-yaml F] [--names N ...] [--slew-index I ...]
        [--load-index I ...] [--constraint-index I ...] [--checks N] [--seed N]

The netlist is --extracted when that file exists, else --schematic, else the
library's own spice. With no --names, the default corners of corners.yaml. The
index options pick a subset of the template's own points. --cell-lib holds the
cell group of a cell the template lib does not have; --reference-cell is the
PDK cell its index points come from.

Exit 0: every corner's tables passed the self-check. 1: a table is off, written
as *.rejected.lib, each point in `violations`. 2: refused, with a remediation.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))
sys.path.insert(0, str(HERE))

import charlib  # noqa: E402
import checklib  # noqa: E402
import corners as corners_mod  # noqa: E402
import layoutlib  # noqa: E402

DEFAULT_LIBRARY = "gf180mcu_fd_sc_mcu7t5v0"
DEFAULT_TEMPLATE = "tt_025C_3v30"


def run(args) -> dict:
    pdk = layoutlib.pdk_root()
    ref = pdk / "libs.ref" / args.library
    template = (Path(args.template_lib) if args.template_lib else
                ref / "lib" / f"{args.library}__{args.template_corner}.lib")
    if not template.is_file():
        raise charlib.Refused("bad_template", f"no template lib at {template}")
    schematic = Path(args.schematic) if args.schematic else (
        ref / "spice" / f"{args.library}.spice")
    data = corners_mod.load(args.corners_yaml or corners_mod.DEFAULT_YAML)
    corners = (corners_mod.corners_by_name(data, args.names) if args.names
               else corners_mod.default_corners(data))
    report = charlib.characterize(
        args.cell, template_lib=template, corners=corners,
        nominal_vdd=args.nominal_vdd, out_dir=Path(args.out), pdk=pdk,
        schematic=schematic, extracted=args.extracted,
        cell_text=(Path(args.cell_lib).read_text(encoding="utf-8")
                   if args.cell_lib else None),
        reference_cell=args.reference_cell, slew_indices=args.slew_index,
        load_indices=args.load_index, constraint_indices=args.constraint_index,
        checks=args.checks, seed=args.seed)
    violations = [
        checklib.violation("characterize", "error", report["netlist"],
                           args.cell, f"table_off_{p['table']}", [name],
                           f"{name}: {p}", "charlib")
        for name, c in report["corners"].items()
        for p in c["check"]["failed"]]
    return {"script": "characterize", "status":
            "pass" if report["ok"] else "violations",
            "counts": {"total": len(violations)}, "violations": violations,
            **report}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--cell", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--extracted")
    ap.add_argument("--schematic")
    ap.add_argument("--library", default=DEFAULT_LIBRARY)
    ap.add_argument("--template-corner", default=DEFAULT_TEMPLATE)
    ap.add_argument("--template-lib")
    ap.add_argument("--cell-lib")
    ap.add_argument("--reference-cell")
    ap.add_argument("--nominal-vdd", type=float, default=3.3)
    ap.add_argument("--corners-yaml")
    ap.add_argument("--names", nargs="+")
    ap.add_argument("--slew-index", type=int, nargs="+")
    ap.add_argument("--load-index", type=int, nargs="+")
    ap.add_argument("--constraint-index", type=int, nargs="+")
    ap.add_argument("--checks", type=int, default=charlib.DEFAULT_CHECKS)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    try:
        return checklib.emit(run(args), args.json)
    except (charlib.Refused, checklib.CheckError, layoutlib.LayoutError) as exc:
        return checklib.emit({"script": "characterize", "status": "error",
                              "kind": getattr(exc, "kind", "error"),
                              "error": str(exc), "remediation": str(exc)},
                             args.json)


if __name__ == "__main__":
    sys.exit(main())
