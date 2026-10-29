"""chip-flow score: how much of the known blocks passes every gate in the Hive.

    chip-flow score       (or: python3 bin/score.py)

The known blocks are the ones blocks/known.json pins (a repository and the
commit chip-flow's block came from): designs that passed their gates once,
which the module must keep passing. Each scores the share of its gate steps
that are clean, and the score is 100 x the mean over the known blocks,
higher is better; 100 is every known block through every gate.

A block under blocks/ is a workspace (a dir with state.json) and, for an
msde block, its analog/ and digital/ sides, each a workspace of its own.
Its gate steps are named `<side>/<gate>` (`top` for the block's own
workspace). They are the steps blocks/<name>/check-all.json records:
`chip-flow check-all --record` re-runs every gate the block records, from
a scratch copy, in the Hive's sandbox through bin/eda, and writes that
record. A step is clean when:

- that run passed it, or failed it under the block's waiver (release then
  judged that waiver, and is a step of its own);
- the run still stands: the block's files, engine/ and bin/eda are as
  they were when it ran (the record's `sources`, a hash of them as git
  tracks them), so a change to a block or to the gates counts only once
  check-all has run over it again and its record is committed with it; and
- no row of docs/workarounds.md names it for that block: its input came
  through a shim in bin/ or an edit made by hand in the block's files.

A known block not under blocks/ yet, or with no record, scores 0. Blocks
under blocks/ that known.json doesn't list are printed, not scored. The
score reads files and hashes them, in seconds; check-all is what runs the gates, and the
test holds docs/workarounds.md to its probes. Prints one line per block,
then the number alone.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KNOWN = ROOT / "blocks" / "known.json"
RECORD = "check-all.json"   # in each block's dir, written by check-all --record
PROBES = ROOT / "tests" / "workarounds"
SIDES = (("analog", "ade"), ("digital", "vde"))
_ROW = re.compile(r"^\|\s*([a-z0-9][a-z0-9-]*)\s*\|(.*)\|\s*$")


@dataclass
class Workaround:
    id: str
    kind: str
    blocks: list[str]
    steps: list[str]

    def charges(self, block: str, step: str) -> bool:
        return ("*" in self.blocks or block in self.blocks) and \
            any(fnmatch(step, s) for s in self.steps)


@dataclass
class Workspace:
    tag: str       # analog, digital, or top (the block's own workspace)
    rel: str       # its path in the block
    skill: str
    gates: list[str] = field(default_factory=list)  # recorded in state.json


@dataclass
class Block:
    name: str
    path: Path
    workspaces: list[Workspace] = field(default_factory=list)

    def steps(self) -> list[str]:
        return [f"{w.tag}/{g}" for w in self.workspaces for g in w.gates]


def read_workarounds(path: Path) -> dict[str, Workaround]:
    """The table's rows by id. A row is `| id | kind | blocks | steps | ...`;
    a step is `<side>/<gate>`, and may be a glob (`digital/*`)."""
    out: dict[str, Workaround] = {}
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = _ROW.match(line)
        if not m or m.group(1) == "id":
            continue
        cells = [c.strip() for c in m.group(2).split("|")]
        if len(cells) < 3:
            raise ValueError(f"{path}:{n}: a row needs id, kind, blocks, steps")
        kind, blocks, steps = cells[0], cells[1], cells[2]
        if kind not in ("shim", "hand"):
            raise ValueError(f"{path}:{n}: kind {kind!r} is not shim or hand")
        st = [] if steps in ("-", "") else [s.strip().strip("`") for s in steps.split(",")]
        for s in st:
            if not re.fullmatch(r"(top|analog|digital|\*)/[a-z_*]+", s):
                raise ValueError(f"{path}:{n}: step {s!r} is not <side>/<gate>")
        if m.group(1) in out:
            raise ValueError(f"{path}:{n}: {m.group(1)} is listed twice")
        out[m.group(1)] = Workaround(m.group(1), kind,
                                     [b.strip() for b in blocks.split(",")], st)
    return out


def _recorded(state: Path) -> list[str]:
    """Every gate the workspace has recorded, passed or failed: one that
    failed stays a step, so a failing run can't drop it from the next."""
    return list(json.loads(state.read_text(encoding="utf-8")).get("gates") or {})


def read_blocks(root: Path) -> list[Block]:
    """Every block under blocks/ (a dir with state.json) and its workspaces,
    sides first: an msde top reads what its sides released."""
    out = []
    for st in sorted(root.glob("*/state.json")):
        d = json.loads(st.read_text(encoding="utf-8"))
        b = Block(d.get("block") or st.parent.name, st.parent)
        if d.get("skill") == "msde":
            for side, skill in SIDES:
                if (st.parent / side / "state.json").is_file():
                    b.workspaces.append(Workspace(side, side, skill,
                                                  _recorded(st.parent / side / "state.json")))
        b.workspaces.append(Workspace("top", ".", d.get("skill") or "", _recorded(st)))
        out.append(b)
    return out


def probes(table: dict[str, Workaround]) -> list[str]:
    """What's wrong between the table and tests/workarounds/: each probe
    exits 0 while its cause stands, 10 once it's fixed, anything else is
    broken. Empty when they agree."""
    out = []
    ids = {p.stem for p in PROBES.glob("*.py") if not p.stem.startswith("_")}
    for i in sorted(set(table) - ids):
        out.append(f"{i}: listed in docs/workarounds.md with no probe tests/workarounds/{i}.py")
    for i in sorted(ids):
        r = subprocess.run([sys.executable, str(PROBES / f"{i}.py")], cwd=ROOT,
                           capture_output=True, text=True)
        why = (r.stdout + r.stderr).strip().splitlines()[-1:] or [""]
        if r.returncode == 0 and i not in table:
            out.append(f"{i}: its cause stands and docs/workarounds.md has no row for it ({why[0]})")
        elif r.returncode == 10 and i in table:
            out.append(f"{i}: fixed ({why[0]}): take the workaround out and delete its row")
        elif r.returncode not in (0, 10):
            out.append(f"{i}: probe broke (exit {r.returncode}): {why[0]}")
    return out


def read_known(path: Path) -> list[dict]:
    """known.json's blocks: each {name, repo, commit}."""
    rows = json.loads(path.read_text(encoding="utf-8")).get("blocks") or []
    for r in rows:
        if not all(isinstance(r.get(k), str) and r[k] for k in ("name", "repo", "commit")):
            raise ValueError(f"{path}: a block needs name, repo and commit: {r}")
    if not rows:
        raise ValueError(f"{path}: no known blocks")
    return rows


def sources(b: Block) -> str:
    """sha256 over what a run of the block's gates reads: the block's files
    and the engine with its launcher (engine/, bin/eda), as git tracks them
    and as they are on disk now, less the record itself. check-all records
    it before it runs."""
    rels = [str(b.path.relative_to(ROOT)), "engine", "bin/eda"]
    files = subprocess.run(["git", "ls-files", "-z", "--", *rels], cwd=ROOT,
                           capture_output=True, check=True).stdout.split(b"\0")
    h = hashlib.sha256()
    for f in sorted(x for x in files if x and not x.endswith(b"/" + RECORD.encode())):
        path = ROOT / f.decode()
        h.update(f + b"\0")
        h.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"-")
    return h.hexdigest()


def block_share(b: Block, table: dict[str, Workaround]) -> tuple[float, str]:
    """The block's clean share of its recorded steps, and a line saying why."""
    try:
        rec = json.loads((b.path / RECORD).read_text(encoding="utf-8"))
    except OSError:
        return 0.0, f"no {RECORD}: run chip-flow check-all --record --block {b.name}"
    steps: dict[str, str] = rec.get("steps") or {}
    if not steps:
        return 0.0, f"{RECORD} records no step"
    if rec.get("sources") != sources(b):
        return 0.0, (f"block, engine/ or bin/eda changed since check-all ran "
                     f"({rec.get('ran', '?')}): run check-all --record --block {b.name}")
    bad = []
    for s, outcome in steps.items():
        why = [] if outcome in ("pass", "waived") else [outcome]
        why += [w.id for w in table.values() if w.charges(b.name, s)]
        if why:
            bad.append(f"{s} ({', '.join(why)})")
    ok = len(steps) - len(bad)
    line = f"{ok}/{len(steps)} clean, run {rec.get('ran', '?')} on {rec.get('toolchain') or '?'}"
    return ok / len(steps), line + (f"; not: {'; '.join(bad)}" if bad else "")


def main() -> int:
    try:
        table = read_workarounds(ROOT / "docs" / "workarounds.md")
        known = read_known(KNOWN)
        blocks = {b.name: b for b in read_blocks(ROOT / "blocks")}
    except (OSError, ValueError) as e:
        print(f"score: {e}", file=sys.stderr)
        return 1
    shares = []
    for k in known:
        b = blocks.get(k["name"])
        if b is None:
            share, line = 0.0, f"not under blocks/ yet ({k['repo']} at {k['commit']})"
        else:
            share, line = block_share(b, table)
        shares.append(share)
        print(f"{k['name']}: {line}")
    for name in sorted(set(blocks) - {k["name"] for k in known}):
        print(f"{name}: not scored (not in blocks/known.json)")
    print(f"known blocks through every gate: {sum(s == 1 for s in shares)}/{len(shares)}")
    print(f"workarounds listed: {len(table)} "
          f"({sum(w.kind == 'shim' for w in table.values())} shim, "
          f"{sum(w.kind == 'hand' for w in table.values())} by hand)")
    print(f"{100 * sum(shares) / len(shares):.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
