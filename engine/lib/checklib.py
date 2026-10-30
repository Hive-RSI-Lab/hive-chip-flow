"""checklib.py - shared plumbing every check script (check_<gate>.py) uses.

Ported from /hwde's scripts/lib/checklib.py (chip-flow docs/design.md 1.3),
domain swapped: a PCB check's finding sits at a board (x, y); a chip-flow
finding sits at a (file, module, line) - a Verilog line, a SPICE netlist
line, a spec.yaml requirement. `cluster_violations.py` groups on that triple
instead of net + spatial radius (design.md 1.3: "the clustering key becomes
file, module and finding kind").

Every check script emits the normalized finding schema:
    {check, severity, file, module, line, kind, refs, msg, source, items}
plus check-specific extra keys downstream consumers (cluster_violations.py,
fixer agents) may read but must not require.

Report payload shape (what gate.py's evaluate() expects from every
check_<gate>.py):
    {script, status: pass|violations|error, counts{total, by_severity,
     by_source}, violations: [...], ...check-specific facts}

Exit contract (docs/design.md 1.1): argparse, JSON to stdout or --out,
exit 0 pass, 1 findings, 2 error with a remediation string, no prompts,
ASCII.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path


class CheckError(RuntimeError):
    """The check cannot run (bad inputs, unusable workspace). CLI exit 2."""


# Version of the report payload shape gate.py's --report validation relies
# on. Bump when a field gate.py depends on changes meaning.
REPORT_SCHEMA = 1

# Version of the CHECKER SEMANTICS behind the reports. Bump on any change to
# what a check finds or how findings are keyed - durable waivers (M3) bind
# this value, so a bump invalidates every recorded waiver until a human
# re-approves it under the new checkers. Coarse on purpose: one bump point,
# conservative direction.
CHECKER_VERSION = 1


def _statelib():
    """Import engine/lib/statelib regardless of whether checklib was
    imported as `checklib` (engine/lib on sys.path) or `lib.checklib`."""
    here = str(Path(__file__).resolve().parent)
    if here not in sys.path:
        sys.path.insert(0, here)
    import statelib
    return statelib


def stamp(payload: dict, input_path) -> dict:
    """Attach the provenance fields gate.py's --report validates: schema
    version, generation time (UTC), input path and its NORMALIZED design
    digest (statelib norms - byte hashes churn on EOL/timestamp noise).
    Digest failures degrade to None (the report stays usable standalone;
    gate.py refuses a report without a digest)."""
    from datetime import datetime, timezone
    payload["report_schema"] = REPORT_SCHEMA
    payload["checker_version"] = CHECKER_VERSION
    payload["generated_at"] = datetime.now(timezone.utc).isoformat(
        timespec="seconds")
    if input_path is not None:
        p = Path(input_path)
        payload["input"] = str(p)
        try:
            sl = _statelib()
            payload["input_digest"] = sl.hash_artifact(p, sl.norm_for_path(p))
        except Exception:
            payload["input_digest"] = None
    return payload


def workspace_relative(obj, ws):
    """obj (a report, any JSON value) with every path under the workspace
    written relative to it: `<ws>/rtl/x.v:16.13` becomes `rtl/x.v:16.13`
    wherever it sits in a string, and `<ws>` itself `.`. gate.py runs each
    check on the resolved workspace, so a tool's own paths (a run dir, a
    yosys src attribute, a sim deck) come back absolute; written as they
    are, a block's committed reports would carry the machine's home path
    and read differently on every machine and scratch copy."""
    roots = sorted({os.path.abspath(ws), str(Path(ws).resolve())},
                   key=len, reverse=True)
    # the root and what follows it: a separator (then the relative path) or
    # nothing a path name goes on with (so `<ws>2/` is left alone)
    rx = re.compile("(" + "|".join(re.escape(r) for r in roots)
                    + r")(/|(?![\w.+-]))")

    def sub(v):
        if isinstance(v, str):
            return rx.sub(lambda m: "" if m.group(2) else ".", v)
        if isinstance(v, list):
            return [sub(x) for x in v]
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        return v
    return sub(obj)


def utf8_stdout() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def rnd(v: float, nd: int = 4) -> float:
    return round(float(v), nd)


def violation(check: str, severity: str, file: str | None,
              module: str | None, kind: str | None, refs, msg: str,
              source: str, line: int | None = None, **extras) -> dict:
    """Build one normalized finding. The clustering key is (file, module,
    kind) - never a spatial position (design.md 1.3)."""
    v = {
        "check": check,
        "severity": severity,
        "file": file,
        "module": module,
        "line": line,
        "kind": kind,
        "refs": sorted(set(refs or [])),
        "msg": msg,
        "source": source,
        "items": [{"msg": msg, "file": file, "line": line}],
    }
    v.update(extras)
    return v


def summarize(violations: list[dict]) -> dict:
    counts = {"total": len(violations)}
    by_sev: dict[str, int] = {}
    by_src: dict[str, int] = {}
    for v in violations:
        by_sev[v["severity"]] = by_sev.get(v["severity"], 0) + 1
        by_src[v["source"]] = by_src.get(v["source"], 0) + 1
    counts["by_severity"] = by_sev
    counts["by_source"] = by_src
    return counts


def report(script: str, workspace, violations: list[dict], **facts) -> dict:
    payload = {
        "script": script,
        "status": "violations" if violations else "pass",
        "counts": summarize(violations),
        "violations": violations,
        **facts,
    }
    return stamp(payload, workspace)


def load_json(path, what: str) -> dict:
    p = Path(path)
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CheckError(f"cannot read {what} {p}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise CheckError(f"{what} {p} is not valid JSON: {exc}") from exc


def emit(payload: dict, out: str | None) -> int:
    """Write/print the report; return the exit code for its status."""
    text = json.dumps(payload, indent=1)
    if out:
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        Path(out).write_text(text, encoding="utf-8")
    else:
        print(text)
    return {"pass": 0, "violations": 1}.get(payload.get("status"), 2)


def cli_wrap(script: str, fn) -> int:
    """Run fn() -> (payload, out_path); map any exception to the exit-2
    error JSON contract, with a `remediation` string alongside `error`."""
    utf8_stdout()
    try:
        payload, out = fn()
    except Exception as exc:  # noqa: BLE001  (contract: any error -> exit 2)
        print(json.dumps({"script": script, "status": "error",
                          "error": f"{type(exc).__name__}: {exc}",
                          "remediation": str(exc)}))
        return 2
    return emit(payload, out)


# A tool that forks its own workers (mcy -> mutate_runner.py -> vvp, sby ->
# solvers) outlives a plain subprocess.run(timeout=): the timeout kills the
# one child it started, and a caller that dies kills nothing. Nine mutate
# sims once ran for two days that way, reparented to init, after their
# pytest runs were gone. bin/eda exec()s every tool through the image's
# ld.so, so the "wrapper" is the tool's own process and passes signals on
# fine; what was missing is killing the whole tree. run_group() puts the
# tool in a process group of its own and SIGKILLs that group when the call
# ends for any reason: exit, timeout, an exception (KeyboardInterrupt too),
# and - through a /bin/sh watchdog holding the read end of a pipe only this
# process writes - this process dying outright, SIGKILL included.
_GUARD = 'read _ ; kill -KILL -"$0" 2>/dev/null'  # dash: no --


def _kill_group(pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# A gate's fixed wall-clock timeout measures the box's load, not the tool:
# pll's transistor-level co-sim takes ~100 s alone and ran past its 240 s
# beside other gates at load 55-170 on 20 cores, so a working tool was
# called a hang. A tool is hung when it stops making progress, so the gates
# time out on that (tests/lib/cpu-cap.sh does the same for the suite):
# progress is a poll seeing the CPU of one of the tool's processes rise
# (counting what each has reaped), a new process, or one of them runnable
# or in disk wait (state R or D: on a starved box a working process can
# wait seconds for a core or the disk and gain no CPU - at IO pressure ~75%
# a python took over 1.5 s just to load - while a waiting one, wedged on a
# peer or a sync point, sleeps in S). No progress for STALL_S is
# a hang; a wall ceiling, many times what the tool takes, stays as a
# backstop for a tool spinning forever. CHIP_FLOW_STALL_S overrides STALL_S.
STALL_S = 180.0
STALL_POLL_S = 2.0


def stall_s() -> float:
    """STALL_S, or CHIP_FLOW_STALL_S when that is a positive number."""
    try:
        v = float(os.environ.get("CHIP_FLOW_STALL_S", ""))
    except ValueError:
        return STALL_S
    return v if v > 0 else STALL_S


class ToolStalled(subprocess.TimeoutExpired):
    """run_group()'s tool stopped making progress for stall seconds. A
    TimeoutExpired, so every caller that handles a timeout handles this."""

    def __init__(self, cmd, stall, elapsed, output=None, stderr=None):
        super().__init__(cmd, stall, output, stderr)
        self.elapsed = elapsed

    def __str__(self):
        return (f"Command '{self.cmd}' made no progress (no CPU used, "
                f"nothing runnable or in disk wait) for {self.timeout:g}s, "
                f"after "
                f"{self.elapsed:.0f}s of wall: it is waiting, not working")


def _proc_tree(pid: int, skip_self: bool = False) -> dict:
    """{(pid, starttime): (ticks, runnable)} for pid and its live
    descendants. ticks are utime+stime+cutime+cstime, so a child that
    finished counts once, in the parent that reaped it. skip_self keeps
    pid's own utime+stime out (the caller's own process, whose polling
    thread would otherwise look like progress)."""
    out: dict = {}
    todo = [pid]
    while todo:
        p = todo.pop()
        try:
            with open(f"/proc/{p}/stat", encoding="ascii",
                      errors="replace") as fh:
                f = fh.read().rsplit(")", 1)[1].split()
            kids = []
            for t in os.listdir(f"/proc/{p}/task"):
                with open(f"/proc/{p}/task/{t}/children",
                          encoding="ascii") as fh:
                    kids += [int(k) for k in fh.read().split()]
        except (OSError, IndexError, ValueError):
            continue
        # f[0] is stat field 3 (state); utime..cstime are fields 14-17,
        # starttime 22
        own = 0 if (skip_self and p == pid) else int(f[11]) + int(f[12])
        out[(p, f[19])] = (own + int(f[13]) + int(f[14]),
                           f[0] in "RD" and not (skip_self and p == pid))
        todo += kids
    return out


def _advanced(before: dict, now: dict) -> bool:
    return any(run or key not in before or ticks > before[key][0]
               for key, (ticks, run) in now.items())


class StallWatch:
    """Tracks a process tree's progress across polls: poll() returns how
    long (s) it has gone without progress."""

    def __init__(self, pid: int, skip_self: bool = False):
        self.pid, self.skip_self = pid, skip_self
        self.last = _proc_tree(pid, skip_self)
        self.since = time.monotonic()

    def poll(self) -> float:
        now = _proc_tree(self.pid, self.skip_self)
        if _advanced(self.last, now):
            self.since = time.monotonic()
        self.last = now
        return time.monotonic() - self.since


def run_group(cmd: list[str], timeout: float | None = None,
              stall: float | None = None,
              **popen_kw) -> subprocess.CompletedProcess:
    """subprocess.run(cmd, timeout=timeout, **popen_kw) for a tool whose
    children must not outlive the call: every process the tool starts dies
    with it, on timeout (TimeoutExpired is raised as subprocess.run does),
    on exit, and when this process is killed. stall, if given, also ends
    it when its tree makes no progress (STALL_S's comment) for that many
    seconds, raising ToolStalled. popen_kw takes what Popen does (cwd,
    capture_output, text, encoding, errors, env, stdin...)."""
    if popen_kw.pop("capture_output", False):
        popen_kw["stdout"] = popen_kw["stderr"] = subprocess.PIPE
    start = time.monotonic()
    proc = subprocess.Popen(cmd, start_new_session=True, **popen_kw)
    # its own session too, so a terminal ^C cannot take the guard down
    # before this process has cleaned up; a clean env, because the caller's
    # may carry bin/eda's LD_PRELOAD, built for the image's libc, not /bin/sh's
    guard = subprocess.Popen(["/bin/sh", "-c", _GUARD, str(proc.pid)],
                             stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True,
                             env={"PATH": "/usr/bin:/bin"})
    deadline = None if timeout is None else start + timeout
    watch = None if stall is None else StallWatch(proc.pid)
    next_poll = start + STALL_POLL_S
    try:
        # communicate() alone would wait for EOF on the pipes, which a
        # backgrounded straggler holds open after the tool itself has exited
        while True:
            left = None if deadline is None else deadline - time.monotonic()
            try:
                out, err = proc.communicate(
                    timeout=0.5 if left is None else max(0.0, min(0.5, left)))
                break
            except subprocess.TimeoutExpired:
                if proc.poll() is not None:
                    _kill_group(proc.pid)
                    out, err = proc.communicate()
                    break
                if left is not None and left <= 0:
                    _kill_group(proc.pid)
                    out, err = proc.communicate()
                    raise subprocess.TimeoutExpired(cmd, timeout, out,
                                                    err) from None
                if watch is not None and time.monotonic() >= next_poll:
                    next_poll = time.monotonic() + STALL_POLL_S
                    if watch.poll() >= stall:
                        _kill_group(proc.pid)
                        out, err = proc.communicate()
                        raise ToolStalled(cmd, stall,
                                          time.monotonic() - start,
                                          out, err) from None
    finally:
        # the guard goes first, so it cannot fire later at a pgid reused
        guard.kill()
        guard.wait()
        guard.stdin.close()
        _kill_group(proc.pid)  # stragglers the leader left behind
        if proc.returncode is None:
            proc.wait()
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def run_tool(cmd: list[str], wall: float,
             **popen_kw) -> subprocess.CompletedProcess:
    """A gate's tool run: run_group() ending it when it stops making
    progress for stall_s() seconds, or at the wall ceiling. Either raises a
    subprocess.TimeoutExpired whose str() says which. A test fakes this,
    not subprocess.run."""
    return run_group(cmd, timeout=wall, stall=stall_s(), **popen_kw)


def timeout_why(exc: subprocess.TimeoutExpired) -> str:
    """One phrase for a gate's error: 'stalled ...' or 'hit its ... wall
    ceiling'."""
    if isinstance(exc, ToolStalled):
        return (f"stalled: no progress (no CPU used, nothing runnable or "
                f"in disk wait) for "
                f"{exc.timeout:g}s after {exc.elapsed:.0f}s")
    return f"timed out at its {exc.timeout:g}s wall ceiling"
