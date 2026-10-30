"""chip-flow check-all: re-run every block's gates inside the Hive.

    chip-flow check-all [--block NAME ...] [--no-probes] [--keep DIR] [--record]

Copies each block under blocks/ to a scratch dir, so the clone stays as it
was, and re-runs there, through engine/scripts/gate.py, every gate each of
its workspaces records in state.json, passed or failed: an msde block's
analog/ (ade) and digital/ (vde) sides first, then its top. Gates run in pipeline
order and record into the scratch copy's state.json, so a later gate
(timing after harden, release after all) reads what this run made, not what
was committed. A gate the workspace's reports/waivers.json waives runs too
and, when it fails, prints as waived: release judges that waiver against
this run's result. Then it runs every probe in tests/workarounds/ against
docs/workarounds.md (score.probes).

With --record, blocks/<name>/check-all.json records each block's run: when,
on which tree, a hash of what it ran from (score.sources: the block's
files, engine/, bin/eda) and each step's outcome (pass, fail, or waived:
failed under a waiver). The block itself stays as it was. That record is
what the score reads (bin/score.py): commit it.

Exit 0 when every step passes and the table matches the probes, else 1.
Run by bin/chip-flow (bin/eda's python, EDA_TOOLCHAIN set).
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
sys.path[:0] = [str(ROOT / "engine" / "scripts"), str(ROOT / "engine" / "lib")]
import score  # noqa: E402  (one reader of blocks/ and docs/workarounds.md)
import state as state_mod  # noqa: E402  (the engine's pipeline order)

EDA = str(ROOT / "bin" / "eda")
GATE = str(ROOT / "engine" / "scripts" / "gate.py")


def run_gate(ws: Path, skill: str, gate: str, logs: Path, tag: str) -> tuple[bool, str]:
    out = logs / f"{tag}-{gate}.json"
    log = logs / f"{tag}-{gate}.log"
    with log.open("w") as f:
        rc = subprocess.run([EDA, "python", GATE, "--gate", gate, "--skill", skill,
                             "--workspace", str(ws), "--out", str(out)],
                            cwd=ws, stdout=f, stderr=subprocess.STDOUT,
                            stdin=subprocess.DEVNULL).returncode
    try:
        res = json.loads(out.read_text())
    except (OSError, ValueError):
        res = {}
    status = res.get("status") or f"exit {rc}"
    if rc == 0 and status == "pass":
        return True, "pass"
    if status == "error":
        err = (str(res.get("error") or "").strip().splitlines() or ["?"])[-1]
        return False, f"error: {err} ({log})"
    if not res:
        tail = log.read_text(errors="replace").strip().splitlines()[-1:] or [""]
        return False, f"{status}: {tail[0]} ({log})"
    msgs = [str(v.get("msg") or v.get("kind")) for v in res.get("failing") or []]
    return False, f"{status}: {res.get('failing_count', '?')} failing" + \
        "".join(f"\n      - {m[:300]}" for m in msgs[:8])


def waivers(ws: Path) -> list[str]:
    """The gates the workspace's reports/waivers.json waives (attest.py)."""
    try:
        data = json.loads((ws / "reports" / "waivers.json").read_text())
    except (OSError, ValueError):
        return []
    return [w["gate"] for w in data if isinstance(w, dict) and w.get("gate")] \
        if isinstance(data, list) else []


def record(b: score.Block, sources: str, outcomes: dict[str, str]) -> None:
    """Write the block's run to blocks/<name>/check-all.json."""
    tree = Path(os.environ.get("EDA_TOOLCHAIN") or "").name or None
    (b.path / score.RECORD).write_text(json.dumps({
        "ran": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "toolchain": tree,
        "sources": sources,
        "steps": outcomes,
    }, indent=1) + "\n", encoding="utf-8")
    print(f"  recorded in {b.path / score.RECORD}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--block", action="append", help="only this block (repeatable)")
    ap.add_argument("--no-probes", action="store_true")
    ap.add_argument("--keep", help="scratch dir to use and keep, for reading after")
    ap.add_argument("--record", action="store_true",
                    help="write each block's run to blocks/<name>/check-all.json, for the score")
    a = ap.parse_args()

    blocks = score.read_blocks(ROOT / "blocks")
    if a.block:
        blocks = [b for b in blocks if b.name in a.block]
        missing = set(a.block) - {b.name for b in blocks}
        if missing:
            print(f"check-all: no block {', '.join(sorted(missing))} under blocks/", file=sys.stderr)
            return 1
    scratch = Path(a.keep) if a.keep else Path(tempfile.mkdtemp(prefix="chip-flow-check-all-"))
    scratch.mkdir(parents=True, exist_ok=True)
    failed = 0
    for b in blocks:
        dst = scratch / b.name
        if dst.exists():
            shutil.rmtree(dst)
        sources = score.sources(b) if a.record else ""
        shutil.copytree(b.path, dst, symlinks=True,
                        ignore=shutil.ignore_patterns("state_snapshots", "__pycache__"))
        logs = scratch / f"{b.name}-logs"
        logs.mkdir(exist_ok=True)
        print(f"{b.name}:", flush=True)
        outcomes: dict[str, str] = {}
        for w in b.workspaces:
            ws = dst / w.rel if w.rel != "." else dst
            order = [g for _, g in state_mod.applicable_gate_order(w.skill)]
            # a gate the block carries a waiver for runs too: release reads
            # its result against the waiver, so it must be this run's.
            waived = waivers(ws)
            for g in sorted(set(w.gates) | set(waived),
                            key=lambda g: order.index(g) if g in order else len(order)):
                t0 = time.monotonic()
                ok, why = run_gate(ws, w.skill, g, logs, w.tag)
                dt = time.monotonic() - t0
                outcomes[f"{w.tag}/{g}"] = "pass" if ok else "waived" if g in waived else "fail"
                if not ok and g in waived:
                    print(f"  waived {w.tag}/{g} ({dt:.0f} s): {why}", flush=True)
                    continue
                print(f"  {'ok  ' if ok else 'FAIL'} {w.tag}/{g} ({dt:.0f} s){'' if ok else ': ' + why}",
                      flush=True)
                failed += not ok
        if a.record:
            record(b, sources, outcomes)
    if not a.no_probes:
        table = score.read_workarounds(ROOT / "docs" / "workarounds.md")
        for p in score.probes(table):
            print(f"  FAIL probe: {p}")
            failed += 1
    if not a.keep:
        shutil.rmtree(scratch, ignore_errors=True)
    print("check-all: ok" if not failed else f"check-all: {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
