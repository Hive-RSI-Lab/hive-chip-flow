#!/usr/bin/env python
"""qor.py - a harden's QoR table, and the diff of two runs (#65).

    qor.py show  <src> [--text] [--out FILE]
    qor.py diff  <new> --against <old>|--against-rev REV [--threshold k=v ...]
    qor.py repro <src>

<src> is a workspace (its recorded harden, reports/recorded/gate-harden.json
or gate-top_harden.json, else harden/runs/run or top/harden/runs/run), a LibreLane run directory, a final/metrics.json, or a
harden report / recorded gate JSON (its `qor` or `facts.qor`). --against-rev REV reads <new>'s
workspace's recorded harden as committed at git REV.

show: exit 0 when every schema metric is there, 1 with a `qor_metric_missing`
finding per missing one (a missing metric is never read as 0).
diff: exit 1 with a `qor_regression` per metric worse than its threshold
(slack/TNS/skew by --threshold ns_abs=0.05 ns, area/power/wirelength/cells by
rel=0.02, DRC/antenna/violation counts by any rise) and a `qor_metric_missing`
per metric the new run lost or the old run lacks (not compared is not clean);
deltas, version and config changes are reported alongside.
repro: exit 1 with `qor_not_reproducible` when the recorded LibreLane, yosys,
OpenROAD or PDK version is not this environment's, or was never recorded.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
REPO = ENGINE.parent
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import qorlib  # noqa: E402
import statelib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "qor"
EDA_BIN = REPO / "bin" / "eda"
RECORDED = Path("reports") / "recorded" / "gate-harden.json"
RECORDED_TOP = Path("reports") / "recorded" / "gate-top_harden.json"


def _from_run(run_dir: Path) -> dict:
    mpath = run_dir / "final" / "metrics.json"
    metrics = checklib.load_json(mpath, "metrics.json")
    harden_dir = run_dir.parent.parent
    cfg = harden_dir / "config.json"
    return qorlib.record(metrics, run_dir, cfg if cfg.is_file() else None)


def _from_obj(obj: dict, what: str) -> dict:
    if isinstance(obj.get("qor"), dict):
        return obj["qor"]
    # gate.py's recorded copy keeps a report's own facts under `facts`
    if isinstance((obj.get("facts") or {}).get("qor"), dict):
        return obj["facts"]["qor"]
    if "metrics" in obj and "corners" in obj:
        return obj
    if any(k.startswith(("design__", "timing__")) for k in obj):
        return qorlib.extract(obj)
    raise CheckError(f"{what} holds no QoR record (no `qor`, not a "
                     "metrics.json) - re-run harden to record one")


def load(src: str) -> tuple[dict, Path | None]:
    """(record, workspace or None)."""
    p = Path(src)
    if p.is_dir():
        for rel in (RECORDED, RECORDED_TOP):
            if (p / rel).is_file():
                return _from_obj(checklib.load_json(p / rel, "harden record"),
                                 str(p / rel)), p
        if (p / "final" / "metrics.json").is_file():
            return _from_run(p), None
        for run in (p / "harden" / "runs" / "run",
                    p / "top" / "harden" / "runs" / "run"):
            if (run / "final" / "metrics.json").is_file():
                return _from_run(run), p
        raise CheckError(f"{p}: no recorded harden ({RECORDED}) and no "
                         "harden/runs/run/final/metrics.json - harden it first")
    if p.is_file():
        if p.name == "metrics.json" and p.parent.name == "final":
            return _from_run(p.parent.parent), None
        return _from_obj(checklib.load_json(p, "QoR source"), str(p)), None
    raise CheckError(f"{src}: no such workspace, run directory or JSON")


def load_rev(ws: Path, rev: str) -> dict:
    ws = ws.resolve()
    top = subprocess.run(["git", "-C", str(ws), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    if top.returncode != 0:
        raise CheckError(f"{ws} is not in a git repository: {top.stderr.strip()}")
    name = RECORDED if (ws / RECORDED).is_file() else RECORDED_TOP
    rel = (ws / name).relative_to(Path(top.stdout.strip()).resolve())
    show = subprocess.run(["git", "-C", str(ws), "show", f"{rev}:{rel.as_posix()}"],
                          capture_output=True, text=True)
    if show.returncode != 0:
        raise CheckError(f"no recorded harden at {rev}:{rel}: "
                         f"{show.stderr.strip()}")
    try:
        return _from_obj(json.loads(show.stdout), f"{rev}:{rel}")
    except ValueError as exc:
        raise CheckError(f"{rev}:{rel} is not JSON: {exc}") from exc


def _missing(names, src, why) -> list[dict]:
    return [checklib.violation(
        "qor", "error", None, None, "qor_metric_missing", [n],
        f"{n}: {why}", "qor", metric=n) for n in names]


def _parse_thresholds(items) -> dict:
    out = {}
    for it in items or []:
        k, _, v = it.partition("=")
        if k not in qorlib.THRESHOLDS or not v:
            raise CheckError(f"--threshold {it!r}: want one of "
                             f"{sorted(qorlib.THRESHOLDS)}=<number>")
        try:
            out[k] = float(v)
        except ValueError as exc:
            raise CheckError(f"--threshold {it!r}: not a number") from exc
    return out


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show")
    s.add_argument("src")
    s.add_argument("--text", action="store_true",
                   help="print the table as text (no JSON)")
    s.add_argument("--out")
    d = sub.add_parser("diff")
    d.add_argument("new")
    g = d.add_mutually_exclusive_group(required=True)
    g.add_argument("--against", help="old workspace/run/metrics/record JSON")
    g.add_argument("--against-rev", help="git rev of <new>'s recorded harden")
    d.add_argument("--threshold", action="append", metavar="K=V")
    d.add_argument("--out")
    r = sub.add_parser("repro")
    r.add_argument("src")
    r.add_argument("--out")
    args = ap.parse_args(argv)

    if args.cmd == "show":
        rec, ws = load(args.src)
        if args.text:
            print(qorlib.table(rec))
            raise SystemExit(1 if rec.get("missing") else 0)
        v = _missing(rec.get("missing") or [], args.src,
                     "the run wrote no such metric (missing, not 0)")
        return checklib.report(SCRIPT, None, v,
                               qor=rec), args.out

    if args.cmd == "diff":
        th = _parse_thresholds(args.threshold)
        new, ws = load(args.new)
        if args.against_rev:
            if ws is None:
                raise CheckError("--against-rev needs <new> to be a workspace")
            old = load_rev(ws, args.against_rev)
        else:
            old, _ = load(args.against)
        res = qorlib.diff(old, new, th)
        v = [checklib.violation(
            "qor", "error", None, None, "qor_regression", [r["metric"]],
            f"{r['metric']}: {r['old']:g} -> {r['new']:g} {r['unit']} "
            f"(better is {r['better']})", "qor", **r)
            for r in res["regressions"]]
        v += _missing(res["lost"], args.new,
                      "the old run recorded it, the new run did not")
        v += _missing(res["not_compared"], args.new,
                      "the old run did not record it, so it was not compared")
        return checklib.report(SCRIPT, None, v, **res), args.out

    rec, ws = load(args.src)
    env = qorlib.env_versions(EDA_BIN)
    why = qorlib.reproducible(rec, env, statelib.pdk_version())
    v = [checklib.violation("qor", "error", None, None, "qor_not_reproducible",
                            [w.split(":", 1)[0]],
                            f"not reproducible as recorded: {w}", "qor")
         for w in why]
    return checklib.report(SCRIPT, None, v,
                           recorded=rec.get("versions"), env=env), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
