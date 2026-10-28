"""chip-flow score: the share of gate steps, over all blocks, with no workaround.

    chip-flow score       (or: python3 bin/score.py)

A block under blocks/ is a workspace (a dir with state.json) and, for an
msde block, its analog/ and digital/ sides, each a workspace of its own. Its
gate steps are the gates each workspace's state.json records as passed,
named `<side>/<gate>` (`top` for the block's own workspace): the steps
`chip-flow check-all` re-runs. A step is charged when a row of
docs/workarounds.md names it for that block: its input came through a shim
in bin/ or an edit made by hand in the block's files. The score is
100 x clean steps / steps, higher is better. It reads files only; check-all
and the test hold the table to the truth (probes, below). Prints one line
per block, then the number alone.
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
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
    gates: list[str] = field(default_factory=list)  # recorded as passed


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


def _passed(state: Path) -> list[str]:
    gates = json.loads(state.read_text(encoding="utf-8")).get("gates") or {}
    return [g for g, r in gates.items() if (r or {}).get("status") == "pass"]


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
                                                  _passed(st.parent / side / "state.json")))
        b.workspaces.append(Workspace("top", ".", d.get("skill") or "", _passed(st)))
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


def main() -> int:
    try:
        table = read_workarounds(ROOT / "docs" / "workarounds.md")
        blocks = read_blocks(ROOT / "blocks")
    except (OSError, ValueError) as e:
        print(f"score: {e}", file=sys.stderr)
        return 1
    total = clean = 0
    for b in blocks:
        steps = b.steps()
        charged = {s: [w.id for w in table.values() if w.charges(b.name, s)] for s in steps}
        ok = [s for s in steps if not charged[s]]
        total += len(steps)
        clean += len(ok)
        bad = [f"{s} ({', '.join(charged[s])})" for s in steps if charged[s]]
        print(f"{b.name}: {len(ok)}/{len(steps)} clean" + (f"; worked around: {'; '.join(bad)}" if bad else ""))
    if not total:
        print("score: no block has a gate step yet", file=sys.stderr)
        return 1
    print(f"workarounds listed: {len(table)} "
          f"({sum(w.kind == 'shim' for w in table.values())} shim, "
          f"{sum(w.kind == 'hand' for w in table.values())} by hand)")
    print(f"{100 * clean / total:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
