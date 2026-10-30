"""chip-flow score: how much of the known blocks passes every gate in the Hive.

    chip-flow score       (or: python3 bin/score.py)

The known blocks are the ones blocks/known.json pins (a repository and the
commit chip-flow's block came from): designs that passed their gates once,
which the module must keep passing. Each scores the share of its gate steps
that are clean, and the score is 100 x the mean over the known blocks,
higher is better; 100 is every known block through every gate.

A block under blocks/ is a workspace (a dir with state.json) and, for an
msde block, its analog/ and digital/ sides, each a workspace of its own.
Its gate steps (`plan`) are named `<side>/<gate>` (`top` for the block's own
workspace): every gate its state.json records, passed or failed, and every
gate its reports/waivers.json waives, in pipeline order. A step is clean
when:

- a run of it on what it reads now passed it, or failed it under the
  block's waiver (release then judged that waiver, and is a step of its
  own). What a step reads is hashed (`step_sources`: the block's files,
  bin/eda and the engine files the gate's run reads, as git tracks them),
  and a step's outcome is looked up by that hash: in the step cache
  (`cache_dir`, outside git), which `chip-flow check-all` fills with every
  step it runs, then in a block's committed check-all.json, a seed from
  `check-all --record`. A step no run covers is not clean: a gate that did
  not run is a refusal, never a pass;
- no row of docs/workarounds.md names it for that block: its input came
  through a shim in bin/ or an edit made by hand in the block's files.

`bin/chip-flow score` first re-runs, through check-all, the blocks with a
step no run covers, within a budget, and fills the cache; then it reads.
`python3 bin/score.py` with CHIP_FLOW_SCORE_FAULT_BUDGET=0 reads only.

A known block not under blocks/ yet scores 0. Blocks under blocks/ that
known.json doesn't list are printed, not scored. The test holds
docs/workarounds.md to its probes.

That pass share is the first of three terms, each out of 100, so the score
keeps showing a gain once every known block is clean:

- pass: 100 x the mean clean share above;
- faults: 100 x the share of the corpus's planted faults (corpus/<skill>/
  <rung>/faults/manifest.yaml) their gates catch, from faults.py's runs,
  cached outside git by a hash of what each rung's run reads and re-run
  only when that changes (bin/score_faults.py); and
- speed: per known block, its clean share x 100 x R / (R + its check-all
  CPU seconds, summed over its steps' runs on what they read now, from
  the cache, else the seed's step_cpu_s), R = SPEED_R, averaged: a
  check-all half as costly scores closer to 100, never past it, and a
  block with a step no timed run covers scores 0 here. CPU, not wall,
  seconds: the machine is shared, and a gate's wall time swings with its
  load.

Prints one line per block and rung, each term, then the score alone.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from fnmatch import fnmatch
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KNOWN = ROOT / "blocks" / "known.json"
RECORD = "check-all.json"   # in each block's dir, written by check-all --record: a seed
PROBES = ROOT / "tests" / "workarounds"
SIDES = (("analog", "ade"), ("digital", "vde"))
SPEED_R = 3600.0   # CPU seconds at which a clean block's speed term is 50
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


def _hash_files(rels: set[str]) -> str:
    h = hashlib.sha256()
    for f in sorted(rels):
        path = ROOT / f
        h.update(f.encode() + b"\0")
        h.update(hashlib.sha256(path.read_bytes()).digest() if path.is_file() else b"-")
    return h.hexdigest()


def _tracked(*rels: str) -> list[str]:
    files = subprocess.run(["git", "ls-files", "-z", "--", *rels], cwd=ROOT,
                           capture_output=True, check=True).stdout.decode().split("\0")
    return [f for f in files if f]


def _block_files(b: Block) -> list[str]:
    return [f for f in _tracked(str(b.path.relative_to(ROOT)))
            if not f.endswith("/" + RECORD)]


def sources(b: Block) -> str:
    """sha256 over everything a run of the block's gates could read: the
    block's files and the whole engine with its launcher (engine/, bin/eda),
    as git tracks them and as they are on disk now, less the record itself.
    A record from before step_sources holds this; step_sources is narrower."""
    return _hash_files(set(_block_files(b)) | set(_tracked("engine", "bin/eda")))


class Engine:
    """What each engine module reads of the engine, from its source: the
    engine modules it imports (anywhere in it, lazily too) or names as a
    file (`"sim_run.py"`, a script it runs), and the data files it names by
    their first path part under engine/<dir>/ (`"gates.yaml"`, `"tt"`,
    `"macro_pdn.tcl"`) in a string other than a docstring. A data file no
    module names is read by every gate: what this can't see counts."""

    def __init__(self) -> None:
        files = _tracked("engine")
        self.mods: dict[str, str] = {}
        data: dict[str, list[str]] = {}   # first part under engine/<dir>/ -> files
        for f in files:
            parts = f.split("/")
            if f.endswith(".py") and len(parts) == 3 and parts[1] in ("scripts", "lib"):
                self.mods[parts[2][:-3]] = f
            elif len(parts) >= 3:
                data.setdefault(parts[2], []).append(f)
            else:
                data.setdefault(f, []).append(f)
        self.data = data
        self.reads: dict[str, set[str]] = {}   # module -> engine files it reads directly
        named: set[str] = set()
        for m, f in self.mods.items():
            mods, keys = self._scan(ROOT / f, data)
            named |= keys
            self.reads[m] = {self.mods[x] for x in mods} | \
                {d for k in keys for d in data[k]}
        self.unnamed = {d for k, fs in data.items() if k not in named for d in fs}

    def _scan(self, path: Path, data: dict) -> tuple[set[str], set[str]]:
        try:
            tree = ast.parse(path.read_bytes())
        except (SyntaxError, ValueError):
            # unreadable as python: it reads all of the engine, say
            return set(self.mods), set(data)
        docs = {id(n.value) for n in ast.walk(tree) if isinstance(n, ast.Expr)
                and isinstance(n.value, ast.Constant) and isinstance(n.value.value, str)}
        mods: set[str] = set()
        keys: set[str] = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                mods |= {a.name.split(".")[0] for a in n.names}
            elif isinstance(n, ast.ImportFrom) and n.module and not n.level:
                mods.add(n.module.split(".")[0])
            elif isinstance(n, ast.Constant) and isinstance(n.value, str) \
                    and id(n) not in docs:
                parts = set(n.value.replace("\\", "/").split("/"))
                mods |= {p[:-3] for p in parts if p.endswith(".py")}
                keys |= parts & set(data)
        return mods & set(self.mods), keys

    def closure(self, roots: list[str]) -> set[str] | None:
        """Every engine file the modules `roots` read, transitively; None
        when one of them is not an engine module."""
        if any(r not in self.mods for r in roots):
            return None
        seen: set[str] = set()
        todo = [self.mods[r] for r in roots]
        by_file = {f: m for m, f in self.mods.items()}
        while todo:
            f = todo.pop()
            if f in seen:
                continue
            seen.add(f)
            if f in by_file:
                todo += [x for x in self.reads[by_file[f]] if x not in seen]
        return seen


def gate_rows(path: Path) -> dict[str, list[tuple[str, str, str]]]:
    """{skill: [(gate, tool, phase)]} from gates.yaml's rows (`gates:
    <skill>: <gate>: tool: <tool>`, `phase: <phase>`), in pipeline order:
    by phase, then as declared (state.applicable_gate_order). Read by
    indentation: the score runs on the host's python, with no yaml."""
    rows: dict[str, dict[str, dict[str, str]]] = {}
    skill = gate = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if m := re.match(r"^  ([a-z_]+):\s*$", line):
            skill, gate = m.group(1), None
            rows.setdefault(skill, {})
        elif (m := re.match(r"^    ([a-z_]+):\s*$", line)) and skill:
            gate = m.group(1)
            rows[skill].setdefault(gate, {})
        elif (m := re.match(r"^      (tool|phase):\s*([A-Za-z0-9_]+)\s*(#.*)?$", line)) \
                and skill and gate:
            rows[skill][gate][m.group(1)] = m.group(2)
    out = {}
    for sk, gs in rows.items():
        order = list(gs)
        out[sk] = [(g, gs[g].get("tool", ""), gs[g].get("phase", "P9")) for g in
                   sorted(order, key=lambda g: (gs[g].get("phase", "P9"), order.index(g)))]
    return out


def gate_tools(path: Path) -> dict[tuple[str, str], str]:
    """{(skill, gate): tool} from gates.yaml (gate_rows)."""
    return {(sk, g): t for sk, rows in gate_rows(path).items() for g, t, _ in rows if t}


def waivers(ws: Path) -> list[str]:
    """The gates the workspace's reports/waivers.json waives (attest.py)."""
    try:
        data = json.loads((ws / "reports" / "waivers.json").read_text())
    except (OSError, ValueError):
        return []
    return [w["gate"] for w in data if isinstance(w, dict) and w.get("gate")] \
        if isinstance(data, list) else []


def plan(b: Block) -> list[tuple[Workspace, str, bool]]:
    """The block's steps in the order check-all runs them: each workspace's
    recorded gates and the gates its waivers name (a waived gate runs too:
    release reads its result against the waiver), sides first, in pipeline
    order; a gate gates.yaml doesn't order goes last, by name. Each is
    (workspace, gate, waived)."""
    rows = gate_rows(ROOT / "engine" / "reference" / "gates.yaml")
    out = []
    for w in b.workspaces:
        order = [g for g, _, _ in rows.get(w.skill, [])]
        waived = waivers(b.path / w.rel)
        out += [(w, g, g in waived) for g in sorted(
            set(w.gates) | set(waived),
            key=lambda g: (order.index(g) if g in order else len(order), g))]
    return out


def cache_root() -> Path:
    """The score's caches, outside git: $CHIP_FLOW_CACHE, else
    $HIVE_SCORE_CACHE (a folder the Hive keeps for a project's score across
    measures, where it gives one), else chip-flow in the user's cache."""
    for k in ("CHIP_FLOW_CACHE", "HIVE_SCORE_CACHE"):
        if os.environ.get(k):
            return Path(os.environ[k])
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "chip-flow"


def cache_dir() -> Path:
    """Where step outcomes are kept: steps/ under cache_root."""
    return cache_root() / "steps"


def _key(block: str, step: str, src: str) -> str:
    return hashlib.sha256(f"{block}\0{step}\0{src}".encode()).hexdigest()


def cached(block: str, step: str, src: str) -> dict | None:
    """The cache's entry for this step run on these inputs, or None."""
    k = _key(block, step, src)
    try:
        e = json.loads((cache_dir() / k[:2] / f"{k}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return e if isinstance(e, dict) and e.get("outcome") in ("pass", "fail", "waived") else None


def cache_put(block: str, step: str, src: str, outcome: str, seconds: float,
              toolchain: str | None, cpu: float | None = None) -> None:
    """Keep a step's outcome on these inputs: pass, fail or waived only (a
    run cut short is no outcome)."""
    k = _key(block, step, src)
    d = cache_dir() / k[:2]
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / f".{k}.{os.getpid()}"
    tmp.write_text(json.dumps({
        "block": block, "step": step, "outcome": outcome, "seconds": round(seconds, 1),
        "cpu_s": None if cpu is None else round(cpu, 1),
        "ran": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "toolchain": toolchain}) + "\n", encoding="utf-8")
    os.replace(tmp, d / f"{k}.json")


def step_sources(b: Block, steps: list[str]) -> dict[str, str]:
    """For each step `<side>/<gate>`, in the order check-all runs them, a
    sha256 over what that run of it reads: the block's files, bin/eda, the
    engine files gate.py and the gate's check_<tool>.py read (Engine), the
    data files no module names, and the same of every step before it that
    could have made its input: the steps before it on its own side, and
    for the top every side's step. An engine change stales only the steps
    that read it; a change to gate.py, a library every gate imports, the
    block or bin/eda stales them all. A gate whose tool can't be found
    reads the whole engine."""
    eng = Engine()
    tools = gate_tools(ROOT / "engine" / "reference" / "gates.yaml")
    skills = {w.tag: w.skill for w in b.workspaces}
    base = set(_block_files(b)) | {"bin/eda"} | eng.unnamed
    whole = set(_tracked("engine"))
    own: dict[str, set[str]] = {}
    out: dict[str, str] = {}
    for s in steps:
        tag, gate = s.split("/", 1)
        tool = tools.get((skills.get(tag, ""), gate))
        files = eng.closure(["gate", f"check_{tool}"]) if tool else None
        own[s] = whole if files is None else files
        reads = set(base)
        for t, f in own.items():
            if tag == "top" or t.split("/", 1)[0] == tag:
                reads |= f
        out[s] = _hash_files(reads)
    return out


def _seed(b: Block) -> dict:
    try:
        rec = json.loads((b.path / RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return rec if isinstance(rec, dict) else {}


def outcomes(b: Block) -> dict[str, str | None]:
    """Each step of the block's plan and its outcome on what it reads now,
    from the cache, else the committed seed; None where no run covers it."""
    steps = [f"{w.tag}/{g}" for w, g, _ in plan(b)]
    now = step_sources(b, steps)
    rec = _seed(b)
    seed = rec.get("steps") if isinstance(rec.get("steps"), dict) else {}
    srcs = rec.get("step_sources")
    # a record from before step_sources: one hash over block, engine, bin/eda
    whole = bool(seed) and not isinstance(srcs, dict) and rec.get("sources") == sources(b)
    out: dict[str, str | None] = {}
    for s in steps:
        e = cached(b.name, s, now[s])
        if e:
            out[s] = e["outcome"]
        elif s in seed and (whole or isinstance(srcs, dict) and srcs.get(s) == now[s]):
            out[s] = seed[s]
        else:
            out[s] = None
    return out


def block_share(b: Block, table: dict[str, Workaround]) -> tuple[float, str]:
    """The block's clean share of its steps, and a line saying why."""
    got = outcomes(b)
    if not got:
        return 0.0, "no gate step: its state.json records no gate"
    bad = []
    unrun = [s for s, o in got.items() if o is None]
    for s, outcome in got.items():
        why = [] if outcome in ("pass", "waived") else ["not run on these inputs"] \
            if outcome is None else [outcome]
        why += [w.id for w in table.values() if w.charges(b.name, s)]
        if why:
            bad.append(f"{s} ({', '.join(why)})")
    ok = len(got) - len(bad)
    line = f"{ok}/{len(got)} clean"
    if unrun:
        line += (f"; {len(unrun)} not run on what they read now "
                 f"(chip-flow score runs them, or check-all --block {b.name})")
    return ok / len(got), line + (f"; not: {'; '.join(bad)}" if bad else "")


def block_cpu(b: Block) -> float | None:
    """The CPU seconds the block's steps took, run on what each reads now:
    from the cache, else the seed's step_cpu_s where its step still stands;
    None when a step has no such time."""
    steps = [f"{w.tag}/{g}" for w, g, _ in plan(b)]
    if not steps:
        return None
    now = step_sources(b, steps)
    rec = _seed(b)
    srcs = rec.get("step_sources") if isinstance(rec.get("step_sources"), dict) else {}
    cpu = rec.get("step_cpu_s") if isinstance(rec.get("step_cpu_s"), dict) else {}
    total = 0.0
    for s in steps:
        e = cached(b.name, s, now[s])
        if e and isinstance(e.get("cpu_s"), (int, float)):
            total += e["cpu_s"]
        elif not e and srcs.get(s) == now[s] and isinstance(cpu.get(s), (int, float)):
            total += cpu[s]
        else:
            return None
    return total


def speed(share: float, cpu: float | None) -> float:
    return 0.0 if cpu is None else share * 100 * SPEED_R / (SPEED_R + cpu)


def main() -> int:
    try:
        table = read_workarounds(ROOT / "docs" / "workarounds.md")
        known = read_known(KNOWN)
        blocks = {b.name: b for b in read_blocks(ROOT / "blocks")}
    except (OSError, ValueError) as e:
        print(f"score: {e}", file=sys.stderr)
        return 1
    shares, speeds = [], []
    for k in known:
        b = blocks.get(k["name"])
        if b is None:
            share, line = 0.0, f"not under blocks/ yet ({k['repo']} at {k['commit']})"
            cpu = None
        else:
            share, line = block_share(b, table)
            cpu = block_cpu(b)
        shares.append(share)
        speeds.append(speed(share, cpu))
        print(f"{k['name']}: {line}; " + ("no step times" if cpu is None else f"{cpu:.0f} CPU s"))
    import score_faults   # it imports this module: here, not at the top
    try:
        caught, planted, rungs = score_faults.fault_share()
    except (OSError, ValueError) as e:
        print(f"score: {e}", file=sys.stderr)
        return 1
    for line in rungs:
        print(line)
    for name in sorted(set(blocks) - {k["name"] for k in known}):
        print(f"{name}: not scored (not in blocks/known.json)")
    print(f"known blocks through every gate: {sum(s == 1 for s in shares)}/{len(shares)}")
    print(f"workarounds listed: {len(table)} "
          f"({sum(w.kind == 'shim' for w in table.values())} shim, "
          f"{sum(w.kind == 'hand' for w in table.values())} by hand)")
    terms = {"pass": 100 * sum(shares) / len(shares),
             "faults": 100 * caught / planted if planted else 0.0,
             "speed": sum(speeds) / len(speeds)}
    print(f"planted faults caught: {caught}/{planted}")
    print("terms: " + ", ".join(f"{k} {v:.2f}" for k, v in terms.items()))
    print(f"{sum(terms.values()):.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
