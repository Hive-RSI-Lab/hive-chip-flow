#!/usr/bin/env python
"""check_mutate.py - the mutate gate (docs/design.md 1.5, "### M2.").

    check_mutate.py --workspace DIR [--size N|auto|all] [--seed N] [--jobs N]
                    [--pdr-timeout S] [--out FILE]

Drives real mcy (yosys's mutation-coverage tool, foss/tools/yosys/bin/mcy -
not through `bin/eda mcy`, which cannot resolve it, docs/design.md 1.2 and
check_env.py's own check_mcy for why; this launches it the same way
check_env.py does, `eda python3 <the real mcy script>`) over a scratch mcy
project under `<workspace>/log/mutate/` (recreated fresh every run - a stale
mutation database is worse than a slower gate). `[script]` reads rtl/
without SYNTHESIS defined (what the sim gate tests, so a PDK cell kept
under `ifdef SYNTHESIS` is not left in the mutants) and elaborates with a
fixed `--top` from spec.yaml; `mutate -list <size> -seed
<seed>` (yosys's own pass, fixed seed) samples the design for candidate
mutations; how many is sample_size() over count_points()'s unlimited
`mutate -list` (spec `mutate.size`: auto, all or a fixed N, see there); `[test sim]` (mutate_runner.py, one process
per mutant) yosys-applies each mutation to the frozen design and runs the
workspace's VISIBLE tb/ cocotb suite against it - PASS = survived, FAIL =
killed. A mutant mcy tags `-mode none` is its own built-in do-nothing
baseline (not a real mutation) and is excluded from every count here.
Every mcy, sby and yosys run goes through checklib.run_group(), so a
timeout, or this process being killed, takes the tool's whole process tree
(mcy's sims included) down with it.

Kill-rate classification (this script's own scheme - gates.yaml names three
must-kill CLASSES but not a mechanism; nothing in mcy exposes a mutation's
own parameters to its `[logic]` section, so classification happens here,
after the run, straight off yosys's own `mutate -list` output strings):
  -mode inv                                  -> condition_inverted
  -mode const0/const1, -wire == "rst"        -> reset_removed  (a fixed,
                                                 project-wide convention: the
                                                 reset port is named 'rst')
  -mode const0/const1, -wire in an output port (spec.yaml `ports`, dir:
                                                 output)         -> output_stuck
  -mode const0/const1, otherwise             -> stuck_other
  -mode cnot0/cnot1                          -> conditional_stuck
  anything else                              -> other

Pass criteria (gates.yaml `mutate` row): kill rate >= 0.9, and no survivor in
{reset_removed, output_stuck, condition_inverted} (severity "error"; a
survivor outside those classes is reported at severity "info" - visible, not
failing). Fault this gate must catch: "a testbench that asserts nothing" (a
tb/ that never fails, however hard the design is mutated, drives kill rate
to ~0 and every survivor class fires) - and, just as fatal in the other
direction, "a testbench that doesn't even run" (an import error, a broken
fixture): every mutant crashes the same way the unmutated design does, mcy's
own `[logic]` counts every crash as a kill, and kill rate reads a false 1.0.
check_baseline() below catches that one, off mcy's own `-none` baseline row
(mutation id 1) rather than off any real mutant.

Formal kills: the visible tests are not the only judge. Each mutant the
tests let survive (formal_kills(), in parallel) is written back out of
mcy's design.il with its mutation applied, the way mutate_runner.py writes
it, and run against the block's formal/*.sv asserts as a bounded sby bmc
(smtbmc yices) at spec.yaml formal.depth, with the frontend and multiclock
mode check_formal.py would pick. An assert that fails within the bound
kills it; anything else (PASS, a timeout, a tool error) leaves it a
survivor, its verdict in `formal_survivors`. Every mutant records
`killed_by` ("sim", "formal", or null for a survivor) and the report counts
`formal_kills`. The step first runs the same bmc on the unmutated design;
unless that passes - and when the workspace has no formal/*.sv, the spec
no formal.depth, or formal.async_reset_cuts is set - no mutant is judged
by formal and `formal_skipped` says why (the kill rate is then the tests'
alone, never higher). A survivor the owner ruled equivalent that a formal
assert kills is refused as a stale ruling (exit 2).

Equivalent mutants: a survivor no test could ever kill - because the
mutation does not change what the design does - is not a testbench gap.
Equivalent here means: started from reset (every FF at zero, or its
declared init), the mutant and the unmutated design give identical PRIMARY
OUTPUTS on every cycle for every input sequence; internal signals may
differ. After the formal kills, each mutant still SURVIVED (in parallel)
gets a proof over a proof (prove_equivalent()): first yosys equiv_induct over every
name-matched signal (zero-init base case; all signals agreeing implies the
outputs do), then, over a miter of the two designs' outputs, k-induction in
yosys (a zero-init base case, then `sat -tempinduct`, the whole yosys
run bounded by EQUIV_TIMEOUT_S), and, only if the base case held but the induction step
did not, an unbounded model check over reachable states (sby `abc pdr`, up
to --pdr-timeout, default PDR_TIMEOUT_S = 30 minutes per mutant). The proof
also assumes a single clock straight from an input and asynchronous resets
acting at clock granularity. When the whole top is outside that clock
model (two clock domains, say) the same proof runs once more on the mutated
module alone - the `-module` mcy names, reference against mutant at that
module's own ports - and a PASS there stands for the top, since a module
whose outputs never differ cannot change anything around it; the reason
then ends "(module <m> alone, at its own ports)". A mutant in the top
module itself gets no such retry. A design or mutant outside the model, a
counterexample, a timeout or a tool error all leave the mutant a normal
survivor. Only a complete proof moves it - or the owner's ruling on that
one mutant: a survivor listed by id under `equivalent` in the workspace's
spec/mutant_rulings.yaml (engine/lib/rulingslib.py, with who ruled and the
evidence) is accepted without being sent to the prover, recorded in
`equivalent_by` as "accepted by owner ruling: <ruling>". A ruling that names
a killed mutant or an id mcy did not generate is refused (exit 2), so a
stale ruling cannot sit silently, and `below_spread` (an analog-only ruling)
is refused here. Kill rate = killed / (total - equivalent), and each proven
or ruled mutant is reported at severity "info" as `equivalent_<class>` (a
must-kill class included - it cannot be killed), with facts `equivalent`
and `equivalent_ids`. If every mutant is equivalent there is nothing left
to score, and that is an error, not a pass.

Survivor causes (survivor_causes(), mutate_probe.py): each survivor left
after the proofs and rulings is given a `cause`, judged on the visible
tests at the same sim seed. One Verilator --coverage-line run of the
unmutated RTL decides `not_activated` (every counted line in the
mutation's narrowest `-src` range reads 0: the tests never reach it - add
stimulus). Every other survivor's netlist and the unmutated one run the
tests under Icarus with a monitor that logs the output ports at each
change; identical logs give `not_propagated` (the change never reaches an
output - drive inputs that carry it out), different ones `not_detected`
with the first differing port and time (an output is wrong and no check
fires - add a checker). A probe that fails or times out gives `unknown`.
The cause rides on the survivor's finding (`cause`, and the route in its
msg) and in facts `survivor_causes`, `survivor_cause_by_id` and
`causes_skipped`; it never changes the verdict.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import json
import math
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
REPO = ENGINE.parent
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import cocotblib  # noqa: E402
import rulingslib  # noqa: E402
import speclib  # noqa: E402
from checklib import CheckError  # noqa: E402
sys.path.insert(0, str(SCRIPTS))
import check_formal  # noqa: E402

SCRIPT = "check_mutate"
EDA_BIN = REPO / "bin" / "eda"
MCY_REL = "foss/tools/yosys/bin/mcy"  # check_env.py's own MCY_REL, mirrored
MUTATE_RUNNER = SCRIPTS / "mutate_runner.py"
MCY_SUBDIR = "log/mutate"
BASELINE_BUILD_ERROR = "baseline_build_error.txt"  # mutate_runner.py writes it
DEFAULT_SIZE = "auto"
# sample sizing (sample_size()): a fixed count was the same 20 for a 12-line
# counter and a 300-line DAC controller. `auto` scores every point of a
# small design, else one mutant per AUTO_LINES_PER_MUTANT RTL lines, never
# under AUTO_FLOOR, never over the wall-budget cap `mutate.max`.
AUTO_FLOOR = 20
AUTO_ALL_MAX = 64        # a design with this many points or fewer: all
AUTO_LINES_PER_MUTANT = 2
# The cap stands for the wall budget. Each mutant is one sim of the
# visible suite plus, if it survives, a bmc and a proof: r2r_dac measured
# 174 s wall for 24 at load 120, so 200 is the most one run should cost.
AUTO_MAX = 200
POINTS_TIMEOUT_S = 120.0
SIZE_WORDS = ("auto", "all")
DEFAULT_SEED = 1
KILL_RATE_MIN = 0.9
RESET_PORT = "rst"
MUST_KILL_CLASSES = {"reset_removed", "output_stuck", "condition_inverted"}
# Wall-clock backstops, so they catch a hang, not a busy disk: mcy keeps its
# database in sqlite, and on a machine whose disk is shared with many jobs
# its commits wait on fsync. r2r_dac's `mcy init` measured 2 s on tmpfs and
# 116 s on the shared ext4 at load 120, with 0.13 s of CPU either way, so
# the old 60 s refused a healthy block.
INIT_TIMEOUT_S = 600.0
RUN_TIMEOUT_S = 600.0  # r2r_dac: 174 s wall, 29 s CPU, at load 120
RUN_TIMEOUT_PER_24 = 24  # mcy run's backstop grows with the sample, per 24
EQUIV_SUBDIR = "equiv"
EQUIV_TIMEOUT_S = 60.0
EQUIV_DEPTH = 8
PDR_TIMEOUT_S = 1800.0
FORMAL_SUBDIR = "formal_kill"
POINTS_SUBDIR = "points"
MUTANT_V_SCRIPT = """\
read_rtlil {design}
{mutation}
techmap
write_verilog -norename {out}
"""
PROVEN = "proven equivalent by "  # + "signal induction", "induction" or "pdr"
# ...and, for a proof scoped to the mutated module alone, + MODULE_SCOPE
RULED = "accepted by owner ruling: "  # + the entry's own `ruling`
# every FF type yosys's own `proc`/`opt` produce with a single CLK edge
# (docs: yosys "Flip-flop cells"); anything else sequential (latches, $sr,
# global-clock $ff, unmapped memories) is outside the proof's clock model.
CLOCKED_FFS = {"$dff", "$dffe", "$adff", "$adffe", "$sdff", "$sdffe",
               "$sdffce", "$aldff", "$aldffe", "$dffsr", "$dffsre"}
SEQ_HINTS = ("dff", "latch", "$sr", "$ff", "$mem", "$fsm")

MODE_RE = re.compile(r"-mode (\S+)")
WIRE_RE = re.compile(r"-wire (\S+)")
SRC_RE = re.compile(r"-src (\S+)")
MODULE_RE = re.compile(r"-module (\S+)")
TOP_IL_RE = re.compile(r"^attribute \\top 1\nmodule (\S+)$", re.M)


def toolchain_root(timeout: float = 30.0) -> Path:
    proc = subprocess.run([str(EDA_BIN), "--print-toolchain-root"],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise CheckError("could not resolve the eda toolchain root: "
                         f"{(proc.stderr or proc.stdout).strip()}")
    return Path(proc.stdout.strip())


def collect_sources(ws: Path) -> list[Path]:
    rtl_dir = ws / "rtl"
    files = sorted(rtl_dir.glob("*.v")) + sorted(rtl_dir.glob("*.sv"))
    if not files:
        raise CheckError(f"no .v/.sv files under {rtl_dir}")
    return checklib.order_sources(files)


def write_config(mcy_dir: Path, rtl_files: list[Path], top: str, tb_dir: Path,
                 size: int, seed: int, sim_seed: int) -> None:
    # -nosynthesis: yosys defines SYNTHESIS by default, but the sim gate
    # (Icarus, no define) tests the `else` side of an `ifdef SYNTHESIS`. A
    # PDK cell kept under SYNTHESIS (a gf180mcu delay cell, say) would
    # otherwise stay in every mutant as a black box Icarus has no model
    # for, and a mutant of the behavioural side is judged by the very
    # tests that passed it.
    reads = "\n".join(f"read_verilog -sv -nosynthesis {f.resolve()}"
                      for f in rtl_files)
    config = f"""\
[options]
size {size}
seed {seed}

[script]
{reads}
hierarchy -top {top}
proc

[logic]
if result("sim") == "FAIL":
    tag("KILLED")
else:
    tag("SURVIVED")

[test sim]
maxbatchsize 1
expect PASS FAIL
run python3 {MUTATE_RUNNER} --tb {tb_dir.resolve()} --top {top} --seed {sim_seed}

[report]
print(f"mutants={{tags()}} killed={{tags('KILLED')}} survived={{tags('SURVIVED')}}")
"""
    (mcy_dir / "config.mcy").write_text(config, encoding="utf-8")


def run_mcy(mcy_dir: Path, mcy_real: Path, nproc: int, size: int = 24
            ) -> None:
    run_timeout = RUN_TIMEOUT_S * max(1, math.ceil(size / RUN_TIMEOUT_PER_24))

    def mcy(*args: str) -> subprocess.CompletedProcess:
        return checklib.run_group(
            [str(EDA_BIN), "python3", str(mcy_real), *args], cwd=str(mcy_dir),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=INIT_TIMEOUT_S if args and args[0] == "init"
            else run_timeout)

    init = mcy("init")
    if init.returncode != 0:
        raise CheckError(f"mcy init failed: "
                         f"{(init.stderr or init.stdout)[-2000:]}")
    ran = mcy("run", "-j", str(max(1, nproc)))
    if ran.returncode != 0:
        raise CheckError(f"mcy run failed: "
                         f"{(ran.stderr or ran.stdout)[-2000:]}")


def rtl_lines(rtl_files: list[Path]) -> int:
    """Lines of RTL that are not blank and not only a `//` comment."""
    n = 0
    for f in rtl_files:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            t = line.strip()
            if t and not t.startswith("//"):
                n += 1
    return n


def count_points(rtl_files: list[Path], top: str, seed: int, work: Path
                 ) -> int:
    """How many mutations yosys's own `mutate -list` offers for this design
    with no limit: the same read and elaboration as write_config()'s
    `[script]`, so it is the population mcy samples from."""
    work.mkdir(parents=True, exist_ok=True)
    out = work / "points.txt"
    reads = "\n".join(f"read_verilog -sv -nosynthesis {f.resolve()}"
                      for f in rtl_files)
    (work / "points.ys").write_text(
        f"{reads}\nhierarchy -top {top}\nproc\n"
        f"mutate -list 100000000 -seed {seed} -o {out.resolve()}\n",
        encoding="utf-8")
    try:
        proc = checklib.run_group(
            [str(EDA_BIN), "yosys", "-q", "-l", "points.log", "-s",
             "points.ys"], cwd=str(work), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=POINTS_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"yosys mutate -list timed out: {exc}") from exc
    if proc.returncode != 0 or not out.is_file():
        raise CheckError("yosys mutate -list failed: "
                         f"{(proc.stderr or proc.stdout)[-1000:]}")
    return sum(1 for line in out.read_text(encoding="utf-8").splitlines()
               if line.strip())


def sample_size(asked, points: int, lines: int, cap) -> tuple[int, str]:
    """(mutant count, rule) for spec.yaml `mutate.size` (or --size):
    an int is a fixed count ("fixed"); "all" is every point ("all");
    "auto" or nothing is every point when there are at most AUTO_ALL_MAX,
    else one per AUTO_LINES_PER_MUTANT RTL lines, at least AUTO_FLOOR, at
    most `cap` (spec `mutate.max`, default AUTO_MAX) ("auto"). Never more
    than the points there are. CheckError for anything else."""
    if points < 1:
        raise CheckError("yosys mutate -list offers no mutation points "
                         "for this design")
    if isinstance(asked, str) and asked.strip().isdigit():
        asked = int(asked)
    if asked is None:
        asked = DEFAULT_SIZE
    if isinstance(asked, bool) or not isinstance(asked, (int, str)):
        raise CheckError(f"mutate.size must be a count, 'auto' or 'all', "
                         f"not {asked!r}")
    if isinstance(asked, int):
        if asked < 1:
            raise CheckError(f"mutate.size must be at least 1, not {asked}")
        return min(asked, points), "fixed"
    if asked == "all":
        return points, "all"
    if asked != "auto":
        raise CheckError(f"mutate.size must be a count, 'auto' or 'all', "
                         f"not {asked!r}")
    if cap is None:
        cap = AUTO_MAX
    if isinstance(cap, bool) or not isinstance(cap, int) or cap < AUTO_FLOOR:
        raise CheckError(f"mutate.max must be a count of at least "
                         f"{AUTO_FLOOR}, not {cap!r}")
    if points <= AUTO_ALL_MAX:
        return points, "auto"
    want = max(AUTO_FLOOR, math.ceil(lines / AUTO_LINES_PER_MUTANT))
    return min(want, cap, points), "auto"


def check_baseline(db_path: Path) -> None:
    """mcy's own `-none` do-nothing baseline is always mutation id 1 (the
    first line `mutate -list ... -none ...` writes to mutations.txt, read
    back in insertion order by mcy's own `init` - see mcy's own script,
    "Importing mutations."). classify()/read_mutants() both treat it as "not
    a mutant" and skip it (module docstring above), so nothing else in this
    script ever looks at its result.

    That baseline result is exactly what a testbench that never even
    imports (a syntax error, a bad fixture) breaks: mutate_runner.py's own
    `[test sim]` step crashes for EVERY mutation, baseline included, and
    mcy's own `[logic]` block (`result("sim")=="FAIL" -> tag("KILLED")`)
    treats every crash as a kill - including the unmutated design's. Kill
    rate over the REAL mutants alone then reads 1.0 (every one "killed",
    for a reason that has nothing to do with the design), and the gate
    passes a tb that tests nothing. This is the one place that baseline's
    own tag is read, so it must PASS (mcy's "SURVIVED": the unmutated
    design behaves, as expected, under the visible tests) before a kill
    rate computed from anything else is trusted at all.

    A baseline that never built (yosys could not write it back out, or
    Icarus refused the netlist) is reported as that, with the tool's own
    error, off the BASELINE_BUILD_ERROR file mutate_runner.py leaves in the
    mcy project directory - not as the tests failing."""
    con = sqlite3.connect(str(db_path))
    try:
        row = con.execute(
            "SELECT mutation FROM mutations WHERE mutation_id = 1").fetchone()
        if row is None:
            raise CheckError("mcy database has no mutation id 1 (expected "
                             "its own '-mode none' baseline row)")
        mode_m = MODE_RE.search(row[0])
        if not mode_m or mode_m.group(1) != "none":
            raise CheckError("mcy mutation id 1 is not the '-mode none' "
                             f"baseline mcy always inserts first (got: "
                             f"{row[0]!r}) - cannot tell whether the "
                             "unmutated design passes the visible tests")
        tags = {t for (t,) in con.execute(
            "SELECT tag FROM tags WHERE mutation_id = 1")}
        if "KILLED" in tags:
            build_error = db_path.parent.parent / BASELINE_BUILD_ERROR
            if build_error.is_file():
                raise CheckError(
                    "the unmutated design did not build, so no test ran: "
                    + build_error.read_text(encoding="utf-8",
                                            errors="replace").strip())
            raise CheckError("the unmutated design fails the visible tests")
        if "SURVIVED" not in tags:
            raise CheckError("mcy's own baseline (mutation id 1) never got "
                             "a KILLED/SURVIVED tag - mcy run did not "
                             "finish cleanly")
    finally:
        con.close()


def classify(mutation: str, output_ports: set[str]) -> str | None:
    mode_m = MODE_RE.search(mutation)
    mode = mode_m.group(1) if mode_m else None
    if mode is None or mode == "none":
        return None  # mcy's own do-nothing baseline row - not a mutant
    wire_m = WIRE_RE.search(mutation)
    wire = wire_m.group(1) if wire_m else None
    if mode == "inv":
        return "condition_inverted"
    if mode in ("const0", "const1"):
        if wire == RESET_PORT:
            return "reset_removed"
        if wire in output_ports:
            return "output_stuck"
        return "stuck_other"
    if mode in ("cnot0", "cnot1"):
        return "conditional_stuck"
    return "other"


def read_mutants(db_path: Path, output_ports: set[str]) -> list[dict]:
    con = sqlite3.connect(str(db_path))
    try:
        mutants = []
        for mid, mutation in con.execute(
                "SELECT mutation_id, mutation FROM mutations"):
            cls = classify(mutation, output_ports)
            if cls is None:
                continue
            tags = {t for (t,) in con.execute(
                "SELECT tag FROM tags WHERE mutation_id = ?", [mid])}
            src_m = SRC_RE.search(mutation)
            mutants.append({
                "id": mid, "mutation": mutation, "class": cls,
                "killed": "KILLED" in tags, "survived": "SURVIVED" in tags,
                "src": src_m.group(1) if src_m else None,
            })
        return mutants
    finally:
        con.close()


EQUIV_SCRIPT = """\
read_rtlil {design}
hierarchy {top}
flatten
rename -top ref
hierarchy -top ref
memory_map
write_json ref.json
write_rtlil ref.il
async2sync
design -stash ref
read_rtlil {design}
hierarchy {top}
{mutation}
flatten
rename -top uut
hierarchy -top uut
memory_map
write_json uut.json
write_rtlil uut.il
async2sync
design -copy-from ref -as ref ref
miter -equiv -flatten -make_assert ref uut miter
hierarchy -top miter
select -assert-min 1 miter/t:$assert
sat -seq {depth} -prove-asserts -set-init-zero -timeout {timeout} -verify miter
log {base_held}
sat -tempinduct -maxsteps {depth} -prove-asserts -set-init-zero -timeout {timeout} -verify miter
"""
BASE_HELD = "CHECK_MUTATE_BASE_CASE_HELD"


def single_clock(netlist: dict) -> tuple | None:
    """(clock bit, polarity) if every sequential cell of the (single,
    flattened) module is an edge-triggered FF on the SAME top-level input
    bit and edge, else None. The proof's sat model steps every FF on one
    implicit global clock and never looks at a CLK net - so a mutant that
    inverts, gates or re-routes a clock, and a design with more than one
    clock domain, is outside what it can decide."""
    mods = netlist.get("modules") or {}
    if len(mods) != 1:
        return None
    mod = next(iter(mods.values()))
    inputs = set()
    for port in (mod.get("ports") or {}).values():
        if port.get("direction") == "input":
            inputs.update(b for b in port.get("bits", []) if isinstance(b, int))
    clocks = set()
    for cell in (mod.get("cells") or {}).values():
        ctype = cell.get("type", "")
        if ctype in CLOCKED_FFS:
            clk = cell.get("connections", {}).get("CLK") or []
            pol = str(cell.get("parameters", {}).get("CLK_POLARITY", ""))
            if len(clk) != 1 or clk[0] not in inputs:
                return None
            clocks.add((clk[0], pol.lstrip("0") or "0"))
        elif any(h in ctype for h in SEQ_HINTS):
            return None
    if len(clocks) > 1:
        return None
    return next(iter(clocks)) if clocks else ("no clock",)


PDR_SBY = """\
[options]
mode prove
multiclock off
timeout {timeout}

[engines]
abc pdr

[script]
read_rtlil ref.il
read_rtlil uut.il
miter -equiv -flatten -make_assert ref uut miter
hierarchy -top miter
async2sync
setundef -zero -init
select -assert-min 1 miter/t:$assert
prep -top miter

[files]
ref.il
uut.il
"""


CLOCK_REFUSED = "not a single, unmodified clock - outside the proof"


def clock_refusal(work: Path) -> str | None:
    """None if ref.json and uut.json (written by EQUIV_SCRIPT) are both
    inside single_clock()'s model on the same clock, else why not."""
    try:
        ref = single_clock(json.loads((work / "ref.json").read_text()))
        uut = single_clock(json.loads((work / "uut.json").read_text()))
    except (OSError, ValueError) as exc:
        return f"could not read the netlists back: {exc}"
    if ref is None or uut is None or ref != uut:
        return CLOCK_REFUSED
    return None


def prove_reachable(work: Path, timeout: float = PDR_TIMEOUT_S
                    ) -> tuple[bool, str]:
    """The stronger proof, over REACHABLE states only: a miter of the
    reference against the mutant (`miter -equiv -make_assert`: every
    output agrees, every cycle), both copies started in the same zero (or
    declared-init) state (`setundef -zero -init`), handed to an unbounded
    model checker (sby prove mode, `abc pdr`, `multiclock off`). PASS means
    no reachable state of the pair tells the two apart through an output.
    Same assumptions as prove_equivalent() - zero init, one clock (its
    caller has already applied single_clock() to both netlists), async
    resets at clock granularity (`async2sync`) - plus that an undefined
    (x) constant reads as zero in both copies. Needs the ref.il/uut.il
    EQUIV_SCRIPT wrote into `work`. Anything short of sby's own PASS - a
    counterexample, a timeout, UNKNOWN, an error - is (False, why)."""
    pdr = work / "pdr"
    shutil.rmtree(pdr, ignore_errors=True)
    pdr.mkdir(parents=True)
    for name in ("ref.il", "uut.il"):
        shutil.copyfile(work / name, pdr / name)
    # sby's own timeout ends it cleanly a little early; the process
    # timeout is the hard bound
    (pdr / "miter.sby").write_text(
        PDR_SBY.format(timeout=max(1, int(timeout) - 5)), encoding="utf-8")
    try:
        proc = checklib.run_group(
            [str(EDA_BIN), "sby", "-f", "miter.sby"], cwd=str(pdr),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"pdr: timeout after {timeout:.0f}s"
    except OSError as exc:
        return False, f"pdr: sby did not start: {exc}"
    out = proc.stdout + proc.stderr
    done = re.findall(r"DONE \((\w+), rc=(\d+)\)", out)
    if proc.returncode == 0 and done and done[-1] == ("PASS", "0"):
        return True, PROVEN + "pdr"
    if done:
        return False, f"pdr: {done[-1][0]}"
    err = [ln for ln in out.splitlines() if "ERROR" in ln]
    return False, (f"pdr: {err[0].strip()}" if err
                   else f"pdr: sby exit {proc.returncode}")


# The first, cheapest route: every name-matched signal (outputs included)
# agrees from reset for N cycles, and equiv_induct shows agreement on all of
# them is inductive. That implies the outputs agree, so it is sound for
# output equivalence; it is only incomplete (an internal difference fails
# it), which the outputs-only miter below then covers.
SIGNAL_EQUIV_SCRIPT = EQUIV_SCRIPT.split("design -copy-from ref")[0] + """\
design -copy-from ref -as ref ref
equiv_make -make_assert ref uut eqa
equiv_make ref uut equiv
select -assert-min 1 eqa/t:$assert
select -assert-min 1 equiv/t:$equiv
sat -seq {depth} -prove-asserts -set-init-zero -timeout {timeout} -verify eqa
equiv_induct -seq {depth} equiv
equiv_status -assert equiv
"""


def prove_signal_induction(design_il: Path, mutation: str, work: Path,
                           timeout: float = EQUIV_TIMEOUT_S,
                           top: str = "-auto-top") -> bool:
    """True when SIGNAL_EQUIV_SCRIPT proves every matched signal equal from
    reset and the clock check holds; any failure, timeout or error is
    False, never a proof."""
    sig = work / "signal"
    sig.mkdir(parents=True, exist_ok=True)
    (sig / "equiv.ys").write_text(SIGNAL_EQUIV_SCRIPT.format(
        top=top, design=design_il.resolve(), mutation=mutation, depth=EQUIV_DEPTH,
        timeout=int(timeout), base_held=BASE_HELD), encoding="utf-8")
    try:
        proc = checklib.run_group(
            [str(EDA_BIN), "yosys", "-q", "-l", "equiv.log", "-s", "equiv.ys"],
            cwd=str(sig), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0 and clock_refusal(sig) is None


MODULE_SCOPE = " (module {} alone, at its own ports)"


def design_top(design_il: Path) -> str | None:
    """The module design.il marks as its top (`attribute \\top 1`), without
    RTLIL's leading backslash; None when it marks none."""
    try:
        m = TOP_IL_RE.search(design_il.read_text(encoding="utf-8",
                                                 errors="replace"))
    except OSError:
        return None
    return m.group(1).lstrip("\\") if m else None


def prove_equivalent(design_il: Path, mutation: str, work: Path,
                     timeout: float = EQUIV_TIMEOUT_S,
                     pdr_timeout: float = PDR_TIMEOUT_S) -> tuple[bool, str]:
    """prove_whole() over the whole top first. When that is not a proof
    and the top is outside the one-clock model (clock_refusal() on the
    top's own netlists - a design with two clock domains, say), the same
    proof runs once more on the mutated module alone (`-module` in the mcy
    mutation string): the reference module against the mutated one, at
    that module's own ports, with that module as the top. mcy mutates the
    module's definition, so every instance of it carries the mutation; if
    every instance's outputs equal the reference's for every input
    sequence, the top around them cannot tell the difference either - so
    a PASS there is a proof for the top. The module's own proof keeps
    every assumption of the whole-top one (its own single clock included),
    and anything short of a PASS leaves the mutant a survivor, reported
    with both reasons. A mutant in the top module itself has no smaller
    scope and gets no second try."""
    proven, why = prove_whole(design_il, mutation, work, timeout,
                              pdr_timeout)
    if proven or clock_refusal(work) != CLOCK_REFUSED:
        return proven, why
    mod_m = MODULE_RE.search(mutation)
    module = mod_m.group(1).lstrip("\\") if mod_m else None
    if not module or module == design_top(design_il):
        return proven, why
    m_proven, m_why = prove_whole(design_il, mutation, work / "module",
                                  timeout, pdr_timeout,
                                  top=f"-top {module}")
    if m_proven:
        return True, m_why + MODULE_SCOPE.format(module)
    return False, f"{why}; module {module} alone: {m_why}"


def prove_whole(design_il: Path, mutation: str, work: Path,
                timeout: float = EQUIV_TIMEOUT_S,
                pdr_timeout: float = PDR_TIMEOUT_S,
                top: str = "-auto-top") -> tuple[bool, str]:
    """Try to PROVE one mutant equivalent to the unmutated design:
    started from reset (every FF at zero, or its declared init), the two
    give the same PRIMARY OUTPUTS on every cycle for every input sequence.
    Internal signals are free to differ - a mutant that changes a register
    no output ever reflects is still equivalent.

    First, prove_signal_induction(): every name-matched signal agrees from
    reset and that agreement is inductive (yosys equiv_induct). That
    implies the outputs agree; it only misses mutants that differ
    internally, which the routes below cover.

    The next two routes check one miter of the two flattened modules (`miter
    -equiv -flatten -make_assert`: an assert per cycle that every output
    agrees). First by k-induction in yosys: a base case (`sat -seq N
    -prove-asserts -set-init-zero`: from reset the outputs agree for N
    cycles) and then `sat -tempinduct -maxsteps N` (N agreeing cycles
    from ANY state imply the next one agrees). Together they are an
    unbounded proof under three stated assumptions: FFs start at zero (or
    their declared init), one clock (single_clock() refuses anything else,
    on both the reference and the mutant), and asynchronous resets act at
    clock granularity (`async2sync`).

    The induction step checks ARBITRARY states, so a mutant that differs
    only on (or only inside) states the design never reaches can fail it.
    Only then - never for a base-case counterexample (a real difference),
    a timeout, an error or a clock refusal - prove_reachable() gets one
    more try over reachable states (pdr_timeout).

    `top` is the `hierarchy` argument that picks the design under proof:
    the whole top by default, `-top <module>` for prove_equivalent()'s
    module-scoped retry.

    Returns (proven, reason); a proof's reason is PROVEN + the method
    ("signal induction", "induction" or "pdr"). Anything short of a full
    proof is (False,
    why), never a proof."""
    work.mkdir(parents=True, exist_ok=True)
    if prove_signal_induction(design_il, mutation, work, timeout, top):
        return True, PROVEN + "signal induction"
    (work / "equiv.ys").write_text(EQUIV_SCRIPT.format(
        top=top, design=design_il.resolve(), mutation=mutation, depth=EQUIV_DEPTH,
        timeout=int(timeout), base_held=BASE_HELD), encoding="utf-8")
    try:
        proc = checklib.run_group(
            [str(EDA_BIN), "yosys", "-q", "-l", "equiv.log", "-s", "equiv.ys"],
            cwd=str(work), capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, f"timeout after {timeout:.0f}s"
    except OSError as exc:
        return False, f"yosys did not start: {exc}"
    if proc.returncode != 0:
        log = work / "equiv.log"
        text = log.read_text(encoding="utf-8", errors="replace") \
            if log.is_file() else ""
        err = [ln for ln in (text or proc.stderr or proc.stdout).splitlines()
               if ln.startswith("ERROR")]
        if BASE_HELD not in text:
            # a base-case counterexample, a solver timeout or a yosys
            # error: nothing the reachable-state proof may override
            if err and "proof did fail" in err[0]:
                return False, "outputs differ from reset (base case)"
            return False, err[0] if err else f"yosys exit {proc.returncode}"
        # the base case held and only the induction step failed
        why = "induction step failed"
        refused = clock_refusal(work)
        if refused:
            return False, refused
        proven, pdr_why = prove_reachable(work, pdr_timeout)
        return proven, pdr_why if proven else f"{why}; {pdr_why}"
    refused = clock_refusal(work)
    if refused:
        return False, refused
    return True, PROVEN + "induction"


def prove_survivors(design_il: Path, mutants: list[dict], work: Path,
                    jobs: int, pdr_timeout: float = PDR_TIMEOUT_S
                    ) -> dict[int, str]:
    """{id: reason} for every SURVIVED mutant, proven or not; the proven
    ones' reason starts with PROVEN. Proofs are independent, so they run
    side by side."""
    todo = [m for m in mutants if m["survived"]]
    if not todo:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max(1, jobs)) as ex:
        futs = {m["id"]: ex.submit(prove_equivalent, design_il, m["mutation"],
                                   work / str(m["id"]),
                                   pdr_timeout=pdr_timeout) for m in todo}
        return {mid: f.result()[1] for mid, f in futs.items()}


def write_mutant_verilog(design_il: Path, mutation: str, work: Path) -> Path:
    """mutant.v under `work`: mcy's frozen design.il with `mutation` applied
    (an empty one for the unmutated baseline), techmapped and written the
    way mutate_runner.py writes the netlist the tests ran. CheckError when
    yosys cannot."""
    work.mkdir(parents=True, exist_ok=True)
    out = work / "mutant.v"
    (work / "mutant.ys").write_text(MUTANT_V_SCRIPT.format(
        design=design_il.resolve(), mutation=mutation, out=out.resolve()),
        encoding="utf-8")
    try:
        proc = checklib.run_group(
            [str(EDA_BIN), "yosys", "-q", "-l", "mutant.log", "-s",
             "mutant.ys"], cwd=str(work), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=EQUIV_TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"yosys writing {out} timed out: {exc}") from exc
    if proc.returncode != 0 or not out.is_file():
        raise CheckError(f"yosys could not write {out}: "
                         f"{(proc.stderr or proc.stdout)[-1000:]}")
    return out


# --- survivor causes (#82c) ---------------------------------------------
# A survivor is a testbench gap, but which one decides the fix. Three
# causes, each judged on the visible tests at the gate's own sim seed:
#   not_activated  - the unmutated design's line coverage (Verilator) never
#                    reaches the mutated line: the tests need stimulus there
#   not_propagated - the line runs, but the mutant's output ports never
#                    differ from the original's: drive inputs that carry
#                    the change out
#   not_detected   - an output differs, yet every test passes: a checker
#                    is missing on that output
# "unknown" when a probe sim fails or times out; the gate never fails on
# a cause, it only routes the survivor's finding.
CAUSES_SUBDIR = "causes"
PROBE = SCRIPTS / "mutate_probe.py"
PROBE_TIMEOUT_S = 900.0
SRC_RANGE_RE = re.compile(r"-src (\S+?):(\d+)\.\d+-(\d+)\.\d+")
CAUSE_ROUTE = {
    "not_activated": "the tests never run line {line} of {file}: add tb "
                     "stimulus that reaches it",
    "not_propagated": "the tests run that code, but the change never "
                      "reaches an output port: drive inputs that carry "
                      "it out",
    "not_detected": "output {port} differs from the original at {t_ns} ns, "
                    "yet every test passes: add a check on {port}",
    "unknown": "cause not found ({error})",
}


def run_probe(argv: list[str], timeout: float = PROBE_TIMEOUT_S) -> dict:
    """mutate_probe.py's own JSON line, or {"ok": False, "error"}."""
    try:
        proc = checklib.run_group(
            [str(EDA_BIN), "python3", str(PROBE), *argv],
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": f"probe timed out after {timeout:.0f} s"}
    for line in reversed(proc.stdout.splitlines()):
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                break
    return {"ok": False, "error": f"probe exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout)[-300:]}"}


def line_hits(info: Path) -> dict[str, dict[int, int]]:
    """{resolved source path: {line: hits}} from the probe's lcov file."""
    import check_cover
    return {str(Path(f).resolve()): rec["line"]
            for f, rec in check_cover.parse_info(info).items()}


def activation(mutation: str, hits: dict[str, dict[int, int]]
               ) -> tuple[bool, str, int] | None:
    """(ran, file, line) for the mutation's narrowest `-src` range that
    holds a counted line (all ranges that narrow together: ran if any of
    their lines was hit), or None when no range holds one."""
    best: list[tuple[str, dict[int, int]]] = []
    best_span = None
    for f, l0, l1 in SRC_RANGE_RE.findall(mutation):
        l0, l1 = int(l0), int(l1)
        lines = hits.get(str(Path(f).resolve()), {})
        da = {n: c for n, c in lines.items() if l0 <= n <= l1}
        if not da:
            continue
        span = l1 - l0
        if best_span is None or span < best_span:
            best, best_span = [(f, da)], span
        elif span == best_span:
            best.append((f, da))
    if not best:
        return None
    ran = any(c > 0 for _, da in best for c in da.values())
    f, da = best[0]
    return ran, Path(f).name, min(da)


def read_trace(path: Path) -> list[tuple[int, tuple[str, ...]]]:
    """The probe's trace with consecutive identical output values merged:
    [(t_ps, values)], one entry per output change."""
    out: list[tuple[int, tuple[str, ...]]] = []
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = raw.split()
        if not parts or not parts[0].isdigit():
            continue
        vals = tuple(parts[1:])
        if out and out[-1][1] == vals:
            continue
        out.append((int(parts[0]), vals))
    return out


def first_divergence(base: list, mut: list, outputs: list[str]
                     ) -> tuple[float, str] | None:
    """(t_ns, port) of the first output change the two merged traces
    disagree on, or None when they are the same."""
    def at(trace, i, t):
        # the trace's values at time t, given entry i is the first after
        # the common prefix
        vals = trace[i - 1][1] if i else None
        if i < len(trace) and trace[i][0] <= t:
            vals = trace[i][1]
        return vals
    n = min(len(base), len(mut))
    i = next((k for k in range(n) if base[k] != mut[k]), n)
    if i == len(base) == len(mut):
        return None
    t = min(tr[i][0] for tr in (base, mut) if i < len(tr))
    a, b = at(base, i, t), at(mut, i, t)
    port = outputs[0]
    if a is not None and b is not None:
        port = next((p for p, x, y in zip(outputs, a, b) if x != y), port)
    return t / 1000.0, port


def survivor_causes(rtl_files: list[Path], top: str, tb_dir: Path,
                    sim_seed: int, design_il: Path, survivors: list[dict],
                    outputs: list[str], work: Path, jobs: int
                    ) -> tuple[dict[int, dict], str | None]:
    """({id: {"cause", ...detail}}, why the coverage step was skipped or
    None). One coverage run of the unmutated RTL settles not_activated;
    every other survivor's outputs are traced against the unmutated
    design's, in parallel."""
    if not survivors:
        return {}, None
    work.mkdir(parents=True, exist_ok=True)
    common = ["--tb", str(tb_dir.resolve()), "--top", top,
              "--seed", str(sim_seed)]
    causes: dict[int, dict] = {}
    cov = run_probe(["cover", *common, "--work", str(work / "cover"),
                     "--src", *[str(f.resolve()) for f in rtl_files]])
    cover_skipped = None
    hits: dict = {}
    if cov.get("ok"):
        try:
            hits = line_hits(Path(cov["info"]))
        except CheckError as exc:
            cover_skipped = str(exc)
    else:
        cover_skipped = cov.get("error", "coverage probe failed")
    todo = []
    for m in survivors:
        act = activation(m["mutation"], hits) if hits else None
        if act is not None and not act[0]:
            causes[m["id"]] = {"cause": "not_activated", "file": act[1],
                               "line": act[2]}
        else:
            todo.append(m)
    if not todo:
        return causes, cover_skipped
    if not outputs:
        for m in todo:
            causes[m["id"]] = {"cause": "unknown",
                               "error": "spec.yaml names no output port"}
        return causes, cover_skipped
    names = ",".join(outputs)

    def traced(mid, mutation):
        sub = work / str(mid)
        try:
            net = write_mutant_verilog(design_il, mutation, sub)
        except CheckError as exc:
            return {"ok": False, "error": str(exc)[:300]}
        return run_probe(["trace", *common, "--work", str(sub / "trace"),
                          "--netlist", str(net), "--watch", names,
                          "--outputs", names])

    with concurrent.futures.ThreadPoolExecutor(max(1, jobs)) as ex:
        base_f = ex.submit(traced, "baseline", "")
        futs = {m["id"]: ex.submit(traced, m["id"], m["mutation"])
                for m in todo}
        base = base_f.result()
        res = {mid: f.result() for mid, f in futs.items()}
    if not base.get("ok"):
        for m in todo:
            causes[m["id"]] = {"cause": "unknown", "error": "the unmutated "
                               f"trace failed: {base.get('error')}"}
        return causes, cover_skipped
    base_tr = read_trace(Path(base["trace"]))
    for mid, r in res.items():
        if not r.get("ok"):
            causes[mid] = {"cause": "unknown", "error": r.get("error")}
            continue
        div = first_divergence(base_tr, read_trace(Path(r["trace"])),
                               outputs)
        causes[mid] = ({"cause": "not_propagated"} if div is None else
                       {"cause": "not_detected", "t_ns": div[0],
                        "port": div[1]})
    return causes, cover_skipped


def cause_route(c: dict) -> str:
    detail = {"line": "?", "file": "?", "port": "?", "t_ns": "?",
              "error": "?", **c}
    return f"{c['cause']}: " + CAUSE_ROUTE[c["cause"]].format(**detail)


def bmc_verdict(work: Path, sv_files: list[Path], mutant_v: Path, plan: dict
                ) -> str:
    """One bounded sby bmc of formal/*.sv over `mutant_v` at the plan's
    depth: "FAIL" (an assert fails within the bound), "PASS", "TIMEOUT"
    or "ERROR: <why>". Never raises on a tool problem - that only leaves
    the mutant where the tests left it."""
    config = work / "bmc.sby"
    check_formal.write_sby(
        config, sv_files, [mutant_v], plan["formal_top"], "bmc",
        "smtbmc yices", plan["depth"], plan["frontend"], plan["slang_so"],
        multiclock=plan["multiclock"], timeout_s=plan["timeout_s"])
    workdir = work / "bmc"
    shutil.rmtree(workdir, ignore_errors=True)
    try:
        proc = checklib.run_group(
            [str(EDA_BIN), "sby", "-f", str(config.resolve()), "-d",
             str(workdir.resolve())], cwd=str(work.resolve()),
            capture_output=True, text=True, encoding="utf-8",
            errors="replace",
            timeout=plan["timeout_s"] + check_formal.SBY_TIMEOUT_GRACE_S)
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
    output = (proc.stdout or "") + (proc.stderr or "")
    status = check_formal.done_status(output)
    if status in ("PASS", "FAIL", "TIMEOUT"):
        return status
    return "ERROR: " + (output.strip().splitlines() or ["no output"])[-1][-300:]


def formal_plan(ws: Path, spec: dict, design_il: Path, work: Path
                ) -> tuple[dict | None, str | None]:
    """(plan, None) when the block's formal/*.sv can judge a mutant, else
    (None, why not). The plan holds formal/*.sv, the formal top, the
    spec's formal.depth and prove timeout, and the frontend and multiclock
    mode check_formal.py would pick, read off the unmutated design written
    the way every mutant is. That baseline must PASS the same bmc, so a
    harness that fails on the design itself never kills a mutant."""
    sv_dir = ws / "formal"
    sv_files = checklib.order_sources(sorted(
        f for ext in (".sv", ".v") for f in sv_dir.glob(f"*{ext}")))
    if not sv_files:
        return None, f"no formal/*.sv in {ws}"
    cfg = spec.get("formal")
    if not isinstance(cfg, dict) or cfg.get("depth") is None:
        return None, "spec.yaml has no formal.depth"
    if cfg.get("async_reset_cuts"):
        return None, ("spec.yaml formal.async_reset_cuts is set, and the "
                      "cut route is not run per mutant")
    try:
        settings = check_formal.formal_settings(spec)
        base_v = write_mutant_verilog(design_il, "", work / "baseline")
        formal_top = cfg.get("top") or f"{spec['top']}_formal"
        frontend, _, slang_so, probe_log = check_formal.pick_frontend(
            sv_files, [base_v], formal_top)
        multiclock, _ = check_formal.needs_multiclock(
            check_formal.clocking(probe_log), spec)
    except CheckError as exc:
        return None, f"formal/*.sv cannot be set up over the mutants: {exc}"
    plan = {"sv_files": sv_files, "formal_top": formal_top,
            "depth": settings["depth"],
            "timeout_s": settings["prove_timeout_s"], "frontend": frontend,
            "slang_so": slang_so, "multiclock": multiclock}
    base = bmc_verdict(work / "baseline", sv_files, base_v, plan)
    if base != "PASS":
        return None, (f"the unmutated design does not pass the bounded "
                      f"formal at depth {plan['depth']} ({base}) - the "
                      "formal gate's own finding; no mutant is judged by it")
    return plan, None


def formal_judge(design_il: Path, mutation: str, work: Path, plan: dict
                 ) -> str:
    """bmc_verdict for one mutant, its yosys step included: a mutant yosys
    cannot write is an "ERROR: ...", never a kill."""
    try:
        mutant_v = write_mutant_verilog(design_il, mutation, work)
    except CheckError as exc:
        return f"ERROR: {exc}"
    return bmc_verdict(work, plan["sv_files"], mutant_v, plan)


def formal_kills(ws: Path, spec: dict, design_il: Path, mutants: list[dict],
                 work: Path, jobs: int) -> tuple[dict[int, str], str | None]:
    """({id: verdict}, why skipped or None) for every mutant the tests let
    survive, each run against formal/*.sv as a bounded bmc at
    formal.depth (side by side). A "FAIL" verdict is a kill."""
    todo = [m for m in mutants if m["survived"]]
    if not todo:
        return {}, None
    plan, skipped = formal_plan(ws, spec, design_il, work)
    if plan is None:
        return {}, skipped
    with concurrent.futures.ThreadPoolExecutor(max(1, jobs)) as ex:
        futs = {m["id"]: ex.submit(formal_judge, design_il, m["mutation"],
                                   work / str(m["id"]), plan) for m in todo}
        return {mid: f.result() for mid, f in futs.items()}, None


def apply_formal_kills(mutants: list[dict], verdicts: dict[int, str],
                       ruled: dict[int, str]) -> None:
    """Record killed_by on every mutant ("sim", "formal" or None for a
    survivor) and move each formal FAIL from survived to killed. A ruled
    mutant a formal assert kills is refused: it is not equivalent, so the
    ruling is stale. Pure."""
    for m in mutants:
        m["killed_by"] = "sim" if m["killed"] else None
        if m["survived"] and verdicts.get(m["id"]) == "FAIL":
            if m["id"] in ruled:
                raise CheckError(
                    f"{rulingslib.RULINGS_REL} equivalent id {m['id']}: a "
                    "formal/*.sv assert fails on that mutant within "
                    "formal.depth, so it is not equivalent - remove the "
                    "stale ruling")
            m.update(killed=True, survived=False, killed_by="formal")


def ruled_survivors(rulings: dict, mutants: list[dict]) -> dict[int, str]:
    """{id: ruling} for every `equivalent` ruling in spec/mutant_rulings.
    yaml, each checked against this run: it must name a mutant mcy
    generated and that SURVIVED, else CheckError - a stale ruling is
    refused, never silently kept."""
    by_id = {m["id"]: m for m in mutants}
    out: dict[int, str] = {}
    for mid, entry in sorted(rulings["equivalent"].items()):
        where = f"{rulingslib.RULINGS_REL} equivalent id {mid}"
        if mid not in by_id:
            raise CheckError(f"{where} is not a mutant this run generated "
                             f"(ids: {', '.join(map(str, sorted(by_id)))}) - "
                             "remove the stale ruling")
        if not by_id[mid]["survived"]:
            raise CheckError(f"{where}: that mutant is killed by the tests "
                             "- remove the stale ruling")
        out[mid] = entry["ruling"]
    return out


def score(mutants: list[dict], equivalent: set[int],
          ruled: dict[int, str] | None = None,
          causes: dict[int, dict] | None = None) -> tuple[dict, list]:
    """The kill-rate arithmetic and findings, kept free of any tool so it
    can be tested alone. A proven-equivalent mutant, or one in `ruled`
    ({id: ruling} - the owner accepted it as equivalent), leaves the
    denominator (no test could ever kill it) and is reported at "info",
    whatever its class. A survivor with an entry in `causes`
    (survivor_causes()) carries its `cause` and the fix it routes to."""
    ruled = ruled or {}
    causes = causes or {}
    equivalent = set(equivalent) | set(ruled)
    scored = [m for m in mutants if m["id"] not in equivalent]
    equiv_ms = [m for m in mutants if m["id"] in equivalent]
    if not scored:
        raise CheckError(f"all {len(mutants)} mutant(s) were proven "
                         "or ruled equivalent to the design - nothing left to score; "
                         "raise spec.yaml mutate.size or change mutate.seed")
    total = len(scored)
    killed = sum(1 for m in scored if m["killed"])
    kill_rate = killed / total

    survivors_by_class: dict[str, list[dict]] = {}
    for m in scored:
        if m["survived"]:
            survivors_by_class.setdefault(m["class"], []).append(m)

    violations = []
    for cls, ms in sorted(survivors_by_class.items()):
        sev = "error" if cls in MUST_KILL_CLASSES else "info"
        for m in ms:
            c = causes.get(m["id"])
            extra = {"cause": c["cause"]} if c else {}
            why = f"; {cause_route(c)}" if c else ""
            violations.append(checklib.violation(
                "mutate", sev, m["src"], None, f"survivor_{cls}", [],
                f"mutant {m['id']} ({cls}) survived{why}: {m['mutation']}",
                "mcy", **extra))
    for m in sorted(equiv_ms, key=lambda m: m["id"]):
        how = (f"accepted as equivalent by owner ruling ({ruled[m['id']]})"
               if m["id"] in ruled else "proven equivalent to the design")
        violations.append(checklib.violation(
            "mutate", "info", m["src"], None, f"equivalent_{m['class']}", [],
            f"mutant {m['id']} ({m['class']}) is {how} and left out of the "
            f"kill rate: {m['mutation']}",
            "mutant_rulings" if m["id"] in ruled else "yosys"))
    if kill_rate < KILL_RATE_MIN:
        violations.append(checklib.violation(
            "mutate", "error", None, None, "kill_rate_below_threshold", [],
            f"kill rate {kill_rate:.2f} ({killed}/{total}) is below "
            f"{KILL_RATE_MIN:.2f}", "mcy"))
    facts = {
        "total_mutants": len(mutants), "equivalent": len(equiv_ms),
        "equivalent_ids": sorted(m["id"] for m in equiv_ms),
        "scored_mutants": total, "killed": killed, "survived": total - killed,
        "kill_rate": round(kill_rate, 4),
        "survivors_by_class": {c: len(v) for c, v in
                               sorted(survivors_by_class.items())},
    }
    return facts, violations


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--size", help="override the mutant count: N, 'auto' "
                    f"or 'all' (default spec.yaml mutate.size, else "
                    f"{DEFAULT_SIZE})")
    ap.add_argument("--seed", type=int, help="override the RNG seed "
                    f"(default {DEFAULT_SEED}, or spec.yaml mutate.seed)")
    ap.add_argument("--sim-seed", type=int, help="override the cocotb "
                    "random seed every mutant's sim runs at (default: "
                    "spec.yaml sim.seed, else a digest of tb/)")
    ap.add_argument("--jobs", type=int, help="parallel mutant runs "
                    "(default: cpu count)")
    ap.add_argument("--pdr-timeout", type=float, default=PDR_TIMEOUT_S,
                    help="seconds of pdr per survivor that induction could "
                    f"not settle (default {PDR_TIMEOUT_S:.0f})")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    return ap.parse_args(argv)


def run(argv=None):
    args = parse_args(argv)

    ws = Path(args.workspace)
    spec = speclib.load_spec(ws / "spec" / "spec.yaml")
    top = spec.get("top")
    if not isinstance(top, str) or not top.strip():
        raise CheckError("spec.yaml has no non-empty 'top' - run spec_lint "
                         "first")
    rtl_files = collect_sources(ws)
    tb_dir = ws / "tb"
    if not cocotblib.test_modules(tb_dir):
        raise CheckError(f"no test_*.py modules under {tb_dir}")

    rulings = rulingslib.load(ws, "mutate")

    mutate_cfg = spec.get("mutate") or {}
    seed = (args.seed if args.seed is not None
           else mutate_cfg.get("seed", DEFAULT_SEED))
    sim_seed = cocotblib.sim_seed(spec, tb_dir, args.sim_seed)
    nproc = args.jobs or os.cpu_count() or 1
    output_ports = {name for name, p in (spec.get("ports") or {}).items()
                   if isinstance(p, dict) and p.get("dir") == "output"}

    t_root = toolchain_root()
    mcy_real = t_root / MCY_REL
    if not mcy_real.is_file():
        raise CheckError(f"mcy not found at {mcy_real}")

    mcy_dir = ws / MCY_SUBDIR
    shutil.rmtree(mcy_dir, ignore_errors=True)
    mcy_dir.mkdir(parents=True)
    t0 = time.monotonic()
    points = count_points(rtl_files, top, seed, mcy_dir / POINTS_SUBDIR)
    lines = rtl_lines(rtl_files)
    asked = (args.size if args.size is not None
             else mutate_cfg.get("size"))
    size, size_rule = sample_size(asked, points, lines,
                                  mutate_cfg.get("max"))
    # mcy's own `size` counts its `-none` baseline row (mcy's reset_status
    # stops at nmutations == size), so it gives size - 1 real mutants. A
    # fixed count keeps meaning mcy's size, so pinned results do not move;
    # all and auto ask mcy for one more, so they score exactly `size`.
    mcy_size = size if size_rule == "fixed" else size + 1
    write_config(mcy_dir, rtl_files, top, tb_dir, mcy_size, seed, sim_seed)
    run_mcy(mcy_dir, mcy_real, nproc, mcy_size)

    db_path = mcy_dir / "database" / "db.sqlite3"
    check_baseline(db_path)
    mutants = read_mutants(db_path, output_ports)
    if not mutants:
        raise CheckError("mcy produced no mutants to score (check spec.yaml "
                         "mutate.size and the design's own size)")
    unresolved = [m for m in mutants if not (m["killed"] or m["survived"])]
    if unresolved:
        raise CheckError(f"{len(unresolved)} of {len(mutants)} mutant(s) "
                         "never got a KILLED/SURVIVED tag - mcy run did not "
                         "finish cleanly")

    ruled = ruled_survivors(rulings, mutants)
    design_il = mcy_dir / "database" / "design.il"
    verdicts, formal_skipped = formal_kills(
        ws, spec, design_il, mutants, mcy_dir / FORMAL_SUBDIR, nproc)
    apply_formal_kills(mutants, verdicts, ruled)
    proofs = prove_survivors(design_il,
                             [m for m in mutants if m["id"] not in ruled],
                             mcy_dir / EQUIV_SUBDIR, nproc, args.pdr_timeout)
    proven_by = {mid: why[len(PROVEN):] for mid, why in proofs.items()
                 if why.startswith(PROVEN)}
    equivalent = set(proven_by)
    survivors = [m for m in mutants if m["survived"]
                 and m["id"] not in equivalent and m["id"] not in ruled]
    causes, causes_skipped = survivor_causes(
        rtl_files, top, tb_dir, sim_seed, design_il, survivors,
        sorted(output_ports), mcy_dir / CAUSES_SUBDIR, nproc)
    facts, violations = score(mutants, equivalent, ruled, causes)
    cause_counts: dict[str, int] = {}
    for c in causes.values():
        cause_counts[c["cause"]] = cause_counts.get(c["cause"], 0) + 1
    equivalent_by = {**proven_by,
                     **{mid: RULED + r for mid, r in ruled.items()}}
    wall_s = time.monotonic() - t0

    # stamp() hashes exactly this path as input_digest; gate.py's own
    # record_gate cross-checks that against invalidation.yaml's gate_inputs
    # kinds[0] for this gate ("rtl" for mutate) - the whole workspace would
    # never match that and silently fail every real recording.
    payload = checklib.report(
        SCRIPT, ws / "rtl", violations, top=top, **facts,
        equivalent_by={str(k): v for k, v in sorted(equivalent_by.items())},
        unproven_survivors={str(k): v for k, v in sorted(proofs.items())
                            if k not in equivalent},
        killed_by={str(m["id"]): m["killed_by"]
                   for m in sorted(mutants, key=lambda m: m["id"])},
        survivor_causes=dict(sorted(cause_counts.items())),
        survivor_cause_by_id={str(k): v for k, v in sorted(causes.items())},
        causes_skipped=causes_skipped,
        formal_kills=sum(1 for m in mutants if m["killed_by"] == "formal"),
        formal_skipped=formal_skipped,
        formal_survivors={str(k): v for k, v in sorted(verdicts.items())
                          if v != "FAIL"},
        points=points, rtl_lines=lines, size=size, size_rule=size_rule,
        sampled_fraction=round(facts["total_mutants"] / points, 4),
        sim_seed=sim_seed,
        sim_replay=cocotblib.replay_command("check_mutate", sim_seed,
                                          "--sim-seed"),
        wall_s=round(wall_s, 2))
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
