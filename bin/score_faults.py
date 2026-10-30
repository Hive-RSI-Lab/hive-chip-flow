"""chip-flow score's planted-fault term: the share of the corpus's planted
faults its gates catch.

Each rung under corpus/<skill>/ with a faults/manifest.yaml plants faults
into a scratch copy of itself, one per entry, and expects the entry's gate
to catch it (engine/scripts/faults.py, the same run tests/test_corpus_faults.py
makes). A fault counts as caught when faults.py's run of its rung finished,
the untouched reference passed every gate the manifest names, and no
problem names that fault. A rung that didn't run, didn't finish, or
whose reference failed catches nothing: a gate that did not run is a
refusal, never a pass.

A rung's run is kept in a cache outside git (CHIP_FLOW_CACHE, else
$XDG_CACHE_HOME/chip-flow, else ~/.cache/chip-flow, under faults/), keyed
by a sha256 over what it reads (fault_sources: the rung's files, bin/eda,
and the engine files faults.py, gate.py and each named gate's check script
read, by score.Engine), so the score re-runs only the rungs a change
touched. Rungs run one at a time through `chip-flow faults`, the ones that
harden last; none starts once CHIP_FLOW_SCORE_FAULT_BUDGET seconds
(default 1800) have gone, and each is killed after
CHIP_FLOW_SCORE_RUNG_TIMEOUT seconds (default 2400). A rung skipped or
killed is printed and is not cached, so a later score runs it.
CHIP_FLOW_SCORE_FAULT_BUDGET=0 reads the cache only.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

import score

CORPUS = score.ROOT / "corpus"
SKILLS = ("vde", "ade", "msde")
CHIP_FLOW = score.ROOT / "bin" / "chip-flow"
HARDENS = {"harden", "top_harden"}


@dataclass
class Rung:
    skill: str
    name: str
    path: Path
    faults: list[tuple[str, str]]   # (fault name, gate), in manifest order

    @property
    def id(self) -> str:
        return f"{self.skill}/{self.name}"

    def hardens(self) -> bool:
        return any(g in HARDENS for _, g in self.faults)


def read_manifest(path: Path) -> list[tuple[str, str]]:
    """The manifest's faults as (name, gate), read by line: the score runs
    on the host's python, with no yaml. An entry is `- name: X` followed by
    its `gate: Y`."""
    out: list[tuple[str, str]] = []
    name = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if m := re.match(r"^\s*-\s+name:\s*([^\s#]+)", line):
            name = m.group(1).strip("'\"")
        elif (m := re.match(r"^\s+gate:\s*([A-Za-z0-9_]+)", line)) and name:
            out.append((name, m.group(1)))
            name = None
    if not out:
        raise ValueError(f"{path}: no faults (each needs `- name:` and `gate:`)")
    return out


def read_rungs(root: Path = CORPUS) -> list[Rung]:
    return [Rung(skill, m.parent.parent.name, m.parent.parent, read_manifest(m))
            for skill in SKILLS
            for m in sorted((root / skill).glob("*/faults/manifest.yaml"))]


def fault_sources(r: Rung, eng: score.Engine | None = None,
                  tools: dict | None = None) -> str:
    """sha256 over what faults.py's run of the rung reads: the rung's files,
    bin/eda, the engine data no module names, and the engine files faults.py,
    gate.py and every named gate's check script read. A gate whose tool
    can't be found reads the whole engine."""
    eng = eng or score.Engine()
    tools = tools if tools is not None else \
        score.gate_tools(score.ROOT / "engine" / "reference" / "gates.yaml")
    reads = set(score._tracked(str(r.path.relative_to(score.ROOT)))) | {"bin/eda"} | eng.unnamed
    roots = ["faults", "gate"]
    for g in sorted({g for _, g in r.faults}):
        tool = tools.get((r.skill, g))
        roots.append(f"check_{tool}" if tool else "")
    files = eng.closure(roots)
    reads |= set(score._tracked("engine")) if files is None else files
    return score._hash_files(reads)


def cache_dir() -> Path:
    d = os.environ.get("CHIP_FLOW_CACHE")
    if d:
        return Path(d) / "faults"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "chip-flow" / "faults"


def caught(r: Rung, out: dict) -> set[str]:
    """The faults faults.py's report shows caught: none when it errored or
    the untouched reference failed, else every fault no problem names."""
    if out.get("status") not in ("pass", "violations"):
        return set()
    msgs = [str(v.get("msg") or "") for v in out.get("violations") or []]
    if any(m.startswith(f"{r.name}: ") for m in msgs):
        return set()
    return {n for n, _ in r.faults if not any(m.startswith(f"{r.name}/{n}:") for m in msgs)}


def run_rung(r: Rung, timeout: float) -> tuple[dict | None, float, str]:
    """faults.py over one rung through chip-flow (its toolchain): its
    report, the wall time, and why there is none."""
    with tempfile.TemporaryDirectory(prefix="chip-flow-score-faults-") as tmp:
        out = Path(tmp) / "faults.json"
        t0 = time.monotonic()
        try:
            p = subprocess.run([str(CHIP_FLOW), "faults", "--skill", r.skill,
                                "--rung", r.name, "--out", str(out)],
                               cwd=score.ROOT, capture_output=True, text=True,
                               stdin=subprocess.DEVNULL, timeout=timeout)
        except subprocess.TimeoutExpired:
            return None, time.monotonic() - t0, f"killed after {timeout:.0f} s"
        dt = time.monotonic() - t0
        try:
            return json.loads(out.read_text(encoding="utf-8")), dt, ""
        except (OSError, ValueError):
            tail = (p.stdout + p.stderr).strip().splitlines()[-1:] or [""]
            return None, dt, f"no report (exit {p.returncode}): {tail[0][:200]}"


def fault_share(rungs: list[Rung] | None = None, budget: float | None = None,
                timeout: float | None = None, runner=run_rung) -> tuple[int, int, list[str]]:
    """(caught, planted, one line per rung): runs the rungs whose cached
    run is stale, within the budget, and counts from the cache."""
    rungs = read_rungs() if rungs is None else rungs
    budget = float(os.environ.get("CHIP_FLOW_SCORE_FAULT_BUDGET", 1800)) if budget is None else budget
    timeout = float(os.environ.get("CHIP_FLOW_SCORE_RUNG_TIMEOUT", 2400)) if timeout is None else timeout
    eng = score.Engine()
    tools = score.gate_tools(score.ROOT / "engine" / "reference" / "gates.yaml")
    cache = cache_dir()
    t0 = time.monotonic()
    got, lines = 0, []
    for r in sorted(rungs, key=lambda r: (r.hardens(), r.id)):
        key = fault_sources(r, eng, tools)
        f = cache / f"{key}.json"
        try:
            rec = json.loads(f.read_text(encoding="utf-8"))
            how = f"cached, {rec.get('wall_s', 0):.0f} s"
        except (OSError, ValueError):
            rec = None
            left = budget - (time.monotonic() - t0)
            if left <= 0:
                lines.append(f"{r.id}: 0/{len(r.faults)} caught: not run (score's fault budget spent)")
                continue
            out, dt, why = runner(r, timeout)
            if out is None:
                lines.append(f"{r.id}: 0/{len(r.faults)} caught: {why}")
                continue
            rec = {"rung": r.id, "ran": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "wall_s": round(dt, 1), "report": out}
            try:
                cache.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps(rec) + "\n", encoding="utf-8")
            except OSError as e:
                print(f"score: can't cache {r.id}: {e}", file=sys.stderr)
            how = f"ran, {dt:.0f} s"
        c = caught(r, rec.get("report") or {})
        got += len(c)
        miss = [n for n, _ in r.faults if n not in c]
        lines.append(f"{r.id}: {len(c)}/{len(r.faults)} caught ({how})"
                     + (f"; missed: {', '.join(miss)}" if miss else ""))
    return got, sum(len(r.faults) for r in rungs), lines
