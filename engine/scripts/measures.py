#!/usr/bin/env python
"""measures.py - every vde `check: measure` bound, read from the records.

    measures.py --workspace DIR [--out FILE]

The table release (check_release.py) gates on, available any time: one row
per bound of every `check: measure` requirement in spec/spec.yaml, with the
value its `source` names in reports/recorded/gate-<gate>.json and a status of
pass, fail, missing or deferred (measurelib.py, issue #89). It reads the
records as they stand: only release also requires them fresh. Not a gate and
records nothing. Exit 0 when no row fails or is missing, 1 when one does, 2
on an error (checklib's contract).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "lib"))
import checklib  # noqa: E402
import measurelib  # noqa: E402
import speclib  # noqa: E402

SCRIPT = "measures"


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)
    ws = Path(args.workspace)
    spec_path = ws / "spec" / "spec.yaml"
    rows = measurelib.evaluate(ws, speclib.load_spec(spec_path))
    violations = [
        checklib.violation(SCRIPT, "error", "spec/spec.yaml", None,
                           "measure_out_of_bounds" if m["status"] == "fail"
                           else "measure_missing",
                           [m["req"]] if m["req"] else [],
                           f"{m['req']} {m['name']} ({m['source']}): {m['why']}",
                           "measurelib")
        for m in rows if m["status"] in ("fail", "missing")]
    return checklib.report(SCRIPT, spec_path, violations, measures=rows), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
