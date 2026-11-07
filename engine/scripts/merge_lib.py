#!/usr/bin/env python
"""merge_lib.py - several cells of one corner in one .lib (charlib.merge_libraries).

    merge_lib.py --out F --name LIBRARY LIB [LIB ...]

Each LIB is a corner .lib from characterize.py. Every header attribute and group
(units, thresholds, voltages, operating conditions) must be the same in all;
templates are kept once by name; a cell in two inputs is refused.

Exit 0: merged. 2: refused (corner_mismatch, template_conflict, duplicate_cell,
nothing_to_merge, unreadable input), with a remediation. Merging has no
findings, so it never exits 1.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))

import charlib  # noqa: E402
import checklib  # noqa: E402

REMEDY = {
    "corner_mismatch": "merge only libs of one corner, characterised from the "
                       "same template (re-run characterize.py for the odd one)",
    "template_conflict": "two inputs define one template name differently; "
                         "re-characterise them with the same charlib",
    "duplicate_cell": "give each cell once",
    "nothing_to_merge": "pass at least one .lib",
    "unreadable": "pass existing .lib files",
}


def run(args) -> dict:
    texts = []
    for p in args.libs:
        try:
            texts.append(Path(p).read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            raise charlib.Refused("unreadable", f"{p}: {exc}") from exc
    merged = charlib.merge_libraries(texts, args.name)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(merged, encoding="utf-8")
    cells = [g.args for g in charlib.parse_liberty(merged).sub("cell")]
    return {"script": "merge_lib", "status": "pass", "lib": str(out),
            "library": args.name, "cells": cells,
            "counts": {"total": 0}, "violations": []}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--name", required=True)
    ap.add_argument("--json")
    ap.add_argument("libs", nargs="*")
    args = ap.parse_args(argv)
    try:
        return checklib.emit(run(args), args.json)
    except charlib.Refused as exc:
        return checklib.emit({"script": "merge_lib", "status": "error",
                              "kind": exc.kind, "error": str(exc),
                              "remediation": f"{exc} - {REMEDY.get(exc.kind, '')}"},
                             args.json)


if __name__ == "__main__":
    sys.exit(main())
