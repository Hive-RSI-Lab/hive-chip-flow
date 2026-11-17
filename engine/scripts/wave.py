#!/usr/bin/env python
"""wave.py - a waves window as text, from an FST or VCD dump (#93).

    chip-flow wave DUMP --signals a,b,c --at T [--window N] [--clock clk]
                        [--scope S] [--out FILE]

DUMP is a .fst (converted with `eda fst2vcd` next to it, as DUMP.vcd) or a
.vcd. --at is the time in ns (a test_failed finding's failure time; the
last row of its `window`). The window is the last N rising edges of
--clock at or before --at, each signal sampled just before the edge, then
one row at --at itself (engine/lib/wavelib.py). --scope defaults to the
shallowest scope holding every --signals name and the clock; a signal may
also be a dotted path below the scope (dut.u_counter8.count).

JSON out: {"window": table text, "rows": [...], "scope", "signals", ...};
exit 0, or 2 with a remediation when the dump or a name is missing.
"""
from __future__ import annotations

import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS.parent / "lib"))
import checklib  # noqa: E402
import wavelib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "wave"


def run(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("dump", help="a .fst or .vcd waves dump")
    ap.add_argument("--signals", required=True,
                    help="comma-separated signal names (or dotted paths "
                    "below the scope)")
    ap.add_argument("--at", type=float, required=True,
                    help="the time in ns the window ends at")
    ap.add_argument("--window", type=int, default=16,
                    help="rising clock edges before --at (default 16)")
    ap.add_argument("--clock", default="clk", help="clock name (default clk)")
    ap.add_argument("--scope", help="scope (dotted) the names are in")
    ap.add_argument("--out", help="write the JSON here instead of stdout")
    args = ap.parse_args(argv)

    dump = Path(args.dump)
    if not dump.is_file():
        raise CheckError(f"no dump at {dump}")
    if dump.suffix == ".fst":
        dump = wavelib.fst_to_vcd(dump, dump.with_suffix(".vcd"))
    sigs = [s.strip() for s in args.signals.split(",") if s.strip()]
    if not sigs:
        raise CheckError("--signals names no signal")
    leaves = {s.rsplit(".", 1)[-1] for s in sigs} | {args.clock}
    vcd = wavelib.parse_vcd(dump,
                            keep=lambda p: p.rsplit(".", 1)[-1] in leaves)
    scope = args.scope
    if scope is None:
        scope = wavelib.block_scope(
            vcd, [args.clock] + [s for s in sigs if "." not in s])
        if scope is None:
            raise CheckError(
                f"no scope in {dump} holds {args.clock} and every one of "
                f"{', '.join(s for s in sigs if '.' not in s)}: pass "
                "--scope, or check the names against the dump's $var lines")
    elif scope not in vcd.scopes:
        raise CheckError(f"no scope {scope!r} in {dump}; scopes: "
                         f"{', '.join(vcd.scopes[:20])}")
    try:
        win = wavelib.window(vcd, scope, args.clock, sigs, args.at * 1000.0,
                             args.window)
    except ValueError as exc:
        raise CheckError(str(exc)) from exc
    missing = [s for s in sigs if s not in win["signals"]]
    if missing:
        raise CheckError(f"not in scope {scope!r}: {', '.join(missing)}")
    payload = {"script": SCRIPT, "status": "pass", "dump": str(dump),
               "window": wavelib.format_window(win), **win}
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
