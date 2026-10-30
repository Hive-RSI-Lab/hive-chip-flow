"""chip-flow check-all: re-run every block's gates inside the Hive.

    chip-flow check-all [--block NAME ...] [--stale] [--budget SECONDS]
                        [--no-probes] [--keep DIR] [--record]

Copies each block under blocks/ to a scratch dir, so the clone stays as it
was, and re-runs there, through engine/scripts/gate.py, every step of its
plan (score.plan): every gate each of its workspaces records in state.json,
passed or failed, and every gate its reports/waivers.json waives: an msde
block's analog/ (ade) and digital/ (vde) sides first, then its top. Gates
run in pipeline order and record into the scratch copy's state.json, so a
later gate (timing after harden, release after all) reads what this run
made, not what was committed. A waived gate that fails prints as waived:
release judges that waiver against this run's result. Then it runs every
probe in tests/workarounds/ against docs/workarounds.md (score.probes).

Each step's outcome (pass, fail, or waived: failed under a waiver) and its
wall and CPU seconds go into the step cache (score.cache_dir, outside git),
keyed by the hash of what the step read (score.step_sources): that is what
the score reads.

--stale runs only the blocks with a step neither the cache nor the block's
committed seed covers, fewest such steps first. --budget stops at that many
seconds: the gate running then is killed and it and every step not reached
stay uncovered, so they count as not clean, never as passed; the exit is 1.
--record also writes blocks/<name>/check-all.json (outcomes, step_sources,
step_wall_s, step_cpu_s), the seed the score falls back on where its cache
is empty: commit it only to seed a fresh cache.

Exit 0 when every step passes and the table matches the probes, else 1.
Run by bin/chip-flow (bin/eda's python, EDA_TOOLCHAIN set).
"""
from __future__ import annotations

import argparse
import json
import os
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(HERE))
import score  # noqa: E402  (one reader of blocks/, the step cache and docs/workarounds.md)

EDA = str(ROOT / "bin" / "eda")
GATE = str(ROOT / "engine" / "scripts" / "gate.py")


class OutOfTime(Exception):
    pass


def run_gate(ws: Path, skill: str, gate: str, logs: Path, tag: str,
             timeout: float | None = None) -> tuple[bool, str]:
    """Run one gate; OutOfTime when it is still running at `timeout`
    seconds (it and its process group are killed)."""
    out = logs / f"{tag}-{gate}.json"
    log = logs / f"{tag}-{gate}.log"
    with log.open("w") as f:
        p = subprocess.Popen([EDA, "python", GATE, "--gate", gate, "--skill", skill,
                              "--workspace", str(ws), "--out", str(out)],
                             cwd=ws, stdout=f, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        try:
            rc = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass
            p.wait()
            raise OutOfTime from None
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


def _child_cpu() -> float:
    """CPU seconds the gates' processes have used so far (user + system)."""
    r = resource.getrusage(resource.RUSAGE_CHILDREN)
    return r.ru_utime + r.ru_stime


def record(b: score.Block, sources: dict[str, str], outcomes: dict[str, str],
           times: dict[str, tuple[float, float]], tree: str | None) -> None:
    """Write the block's run to blocks/<name>/check-all.json, a seed."""
    (b.path / score.RECORD).write_text(json.dumps({
        "ran": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "toolchain": tree,
        "steps": outcomes,
        "step_sources": {s: sources[s] for s in outcomes},
        "step_wall_s": {s: times[s][0] for s in outcomes},
        "step_cpu_s": {s: times[s][1] for s in outcomes},
    }, indent=1) + "\n", encoding="utf-8")
    print(f"  recorded in {b.path / score.RECORD}", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--block", action="append", help="only this block (repeatable)")
    ap.add_argument("--no-probes", action="store_true")
    ap.add_argument("--keep", help="scratch dir to use and keep, for reading after")
    ap.add_argument("--record", action="store_true",
                    help="also write each block's run to blocks/<name>/check-all.json, a seed")
    ap.add_argument("--stale", action="store_true",
                    help="only the blocks with a step no cached or committed run covers")
    ap.add_argument("--budget", type=float,
                    help="stop after this many seconds; what didn't run stays not clean")
    a = ap.parse_args()

    blocks = score.read_blocks(ROOT / "blocks")
    if a.block:
        blocks = [b for b in blocks if b.name in a.block]
        missing = set(a.block) - {b.name for b in blocks}
        if missing:
            print(f"check-all: no block {', '.join(sorted(missing))} under blocks/", file=sys.stderr)
            return 1
    if a.stale:
        todo = []
        for b in blocks:
            n = sum(o is None for o in score.outcomes(b).values())
            if n:
                todo.append((n, b))
            else:
                print(f"{b.name}: every step covered on what it reads now", flush=True)
        blocks = [b for _, b in sorted(todo, key=lambda x: (x[0], x[1].name))]
    deadline = time.monotonic() + a.budget if a.budget else None
    tree = Path(os.environ.get("EDA_TOOLCHAIN") or "").name or None
    scratch = Path(a.keep) if a.keep else Path(tempfile.mkdtemp(prefix="chip-flow-check-all-"))
    scratch.mkdir(parents=True, exist_ok=True)
    failed = 0
    for b in blocks:
        if deadline and time.monotonic() >= deadline:
            print(f"{b.name}: not run, out of budget ({a.budget:.0f} s)", flush=True)
            failed += 1
            continue
        dst = scratch / b.name
        if dst.exists():
            shutil.rmtree(dst)
        plan = score.plan(b)
        # what each step reads, hashed before anything runs
        sources = score.step_sources(b, [f"{w.tag}/{g}" for w, g, _ in plan])
        shutil.copytree(b.path, dst, symlinks=True,
                        ignore=shutil.ignore_patterns("state_snapshots", "__pycache__"))
        logs = scratch / f"{b.name}-logs"
        logs.mkdir(exist_ok=True)
        print(f"{b.name}:", flush=True)
        outcomes: dict[str, str] = {}
        times: dict[str, tuple[float, float]] = {}
        for w, g, waived in plan:
            step = f"{w.tag}/{g}"
            ws = dst / w.rel if w.rel != "." else dst
            left = deadline - time.monotonic() if deadline else None
            t0, c0 = time.monotonic(), _child_cpu()
            try:
                if left is not None and left <= 0:
                    raise OutOfTime
                ok, why = run_gate(ws, w.skill, g, logs, w.tag, left)
            except OutOfTime:
                print(f"  out of budget at {step}: it and the steps after it did not run",
                      flush=True)
                failed += 1
                break
            dt = time.monotonic() - t0
            times[step] = (round(dt, 1), round(_child_cpu() - c0, 1))
            outcomes[step] = "pass" if ok else "waived" if waived else "fail"
            score.cache_put(b.name, step, sources[step], outcomes[step], dt, tree,
                            cpu=times[step][1])
            if not ok and waived:
                print(f"  waived {step} ({dt:.0f} s): {why}", flush=True)
                continue
            print(f"  {'ok  ' if ok else 'FAIL'} {step} ({dt:.0f} s){'' if ok else ': ' + why}",
                  flush=True)
            failed += not ok
        if a.record and len(outcomes) == len(plan):
            record(b, sources, outcomes, times, tree)
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
