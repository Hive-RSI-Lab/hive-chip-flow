#!/usr/bin/env python
"""check_lec.py - the lec gate: RTL = synth netlist = final/nl, proved.

    check_lec.py --workspace DIR [--out FILE]

glsim simulates what the tests drive; a resizer or ECO edit on a cone they
never sensitise gets through it. This proves two links for every input:

  rtl_vs_synth    rtl/ and the TT wrapper against LibreLane's own synthesis
                  output (harden/runs/run/*-yosys-synthesis/, the netlist the
                  layout was made from, not synth/, which is check_synth's
                  separate abc run)
  synth_vs_final  that netlist against harden/runs/run/final/nl

A run dir trimmed of the synth view is a refusal, not a shorter proof: CTS
renames the Q net of a flop that drives a clock, so RTL can't be matched to
final/nl by name, and it would report differences that aren't there.

Each link goes through engine/lib/leclib.py, whose docstring says why the flops
become ports. One combinational miter proves every key-point pin and output
of a link at once. ABC's cec tries it first, as one 2-valued AIG with gold's
x bits tied to 0 (one of the circuits gold allows, so gate equal to it is a
pass); it proves riscv's links in seconds where sat takes hours. It can only
pass: anything else goes to the yosys sat miter, which finds the points that
differ, and eqy, one partition per point, runs only on those, to name each
with its own counterexample. `prover` says which one proved the link.
Cell functions come from the same liberty as timing. Fill, tap, decap and
antenna cells have no output so they vanish; buffers and delay cells are
their function.

State synthesis moved is proved, not matched by name:

  constants       a flop only gold has, which synthesis dropped as constant,
                  is proved constant by induction (its value in, the same
                  value out, the opposite reset never firing, all candidates
                  together) before it is tied off. The links' `constants`
                  lists them. A flop dropped that is not constant fails.
  fsm             on rtl_vs_synth, each state map fsm_recode wrote in
                  synthesis's log (*-yosys-synthesis/*.log) is read, and the
                  recoded register is compared through it: both views get one
                  free state index, gold's flops take the old code and the
                  netlist's the new, and each netlist state flop's D, clock
                  and reset are compared with gold's next state put through
                  the map. `fsm` lists each map and whether it applied; one
                  that can't be applied leaves the register to fail by name.

kepler-formal is the second engine on the netlist link. It compares D, CLK and
reset cones by instance name, so it can't see a flop's type: it passes a flop
swapped for its negedge or set twin, which eqy here fails. Agreement is
therefore one-sided: kepler finding a difference eqy proved absent is a
finding, and kepler seeing none where eqy failed is reported, not a finding.
kepler refusing to compare (renamed instance, cloned flop) is reported too.

Findings:

  lec_not_equivalent    a key-point pin or output differs, with a counterexample
  lec_unproven          the miter did not finish (timeout). Not a pass.
  lec_unmodelled_cell   a used cell has no function in liberty (clock gate,
                        tri-state). No model, no proof: the gate stops there,
                        with `links` empty, and has proved nothing.
  lec_engines_disagree  kepler-formal found a difference eqy proved absent

Out of scope, in the report's `not_checked`: power-up values (`initial` and
init attributes; `rtl_init` says if the RTL has any; a proved constant holds
from the first time it has its value), timing, and retiming, which moves
state with no map to read and fails rather than passing. `unmatched` lists
key points in one view only: those pass only if nothing observable reads them
(a duplicate flop the synthesiser merged).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
REPO = ENGINE.parent
sys.path.insert(0, str(ENGINE / "lib"))
sys.path.insert(0, str(SCRIPTS))
import check_synth  # noqa: E402
import checklib  # noqa: E402
import leclib  # noqa: E402
import speclib  # noqa: E402
import ttlib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "check_lec"
EDA_BIN = REPO / "bin" / "eda"
TIMEOUT_S = 1800.0
PARTITION_TIMEOUT_S = 120
MITER_TIMEOUT_S = 1700
MITER_ROUNDS = 4
ABC_TIMEOUT_S = 600
NAME_MAX = 64
JOBS = "2"
NOT_CHECKED = ["power-up values (no initial state exists in silicon)",
               "timing", "retiming (it fails, not passes)"]


def _tool(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return checklib.run_tool(cmd, cwd=str(cwd), capture_output=True, text=True,
                             encoding="utf-8", errors="replace",
                             wall=TIMEOUT_S)


def write_view(name: str, reads: list[str], top: str, liberty: Path,
               work: Path, key_by: str) -> leclib.View:
    (work / "lec_cells.v").write_text(leclib.LEC_CELLS, encoding="utf-8")
    (work / "lec_map.v").write_text(leclib.LEC_MAP, encoding="utf-8")
    raw = work / f"{name}.raw.il"
    script = work / f"{name}.ys"
    script.write_text(leclib.view_script(
        reads, top, str(liberty), "lec_cells.v", "lec_map.v", raw.name,
        f"{name}.aliases.json", key_by),
        encoding="utf-8")
    try:
        proc = _tool([str(EDA_BIN), "yosys", "-q", "-l", f"{name}.log",
                      "-s", script.name], work)
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"yosys on {name}: {checklib.timeout_why(exc)}") from exc
    if proc.returncode != 0:
        raise CheckError(f"yosys on {name} exited {proc.returncode}: "
                         f"{((proc.stdout or '') + (proc.stderr or ''))[-1500:]}")
    try:
        return leclib.parse_view(raw.read_text(encoding="utf-8"), top, key_by)
    except leclib.LecError as exc:
        raise CheckError(f"{name}: {exc}") from exc


def _yosys_sat(script: str, name: str, work: Path) -> str | None:
    """Run one yosys script; its log, or None if the wall clock ran out."""
    (work / f"{name}.ys").write_text(script, encoding="utf-8")
    try:
        proc = _tool([str(EDA_BIN), "yosys", "-q", "-l", f"{name}.log",
                      "-s", f"{name}.ys"], work)
    except subprocess.TimeoutExpired:
        return None
    log = (work / f"{name}.log")
    text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    if proc.returncode != 0 and "model found" not in text:
        raise CheckError(f"yosys {name} exited {proc.returncode}: "
                         f"{((proc.stdout or '') + (proc.stderr or ''))[-1500:]}")
    return text


def run_miter(top: str, work: Path, gold: leclib.View, gate: leclib.View,
              compare: set[str], name: str) -> tuple[str, list[str], dict]:
    (work / f"{name}.gold.il").write_text(
        leclib.render(gold, set(gate.key_points), compare), encoding="utf-8")
    (work / f"{name}.gate.il").write_text(
        leclib.render(gate, set(gold.key_points), compare), encoding="utf-8")
    log = _yosys_sat(leclib.miter_script(
        top, f"{name}.gold.il", f"{name}.gate.il", MITER_TIMEOUT_S), name, work)
    if log is None:
        return "unknown", [], {}
    return leclib.miter_verdict(log)


def abc_proves(top: str, work: Path, gold: leclib.View, gate: leclib.View,
               compare: set[str]) -> bool:
    """ABC's cec on the link's miter: True only if it proved it equal. Any
    other answer (a difference, a timeout, a tool error) leaves the link to
    the sat miter, so this can only save time, never pass a link."""
    name = "abc"
    (work / f"{name}.gold.il").write_text(
        leclib.render(gold, set(gate.key_points), compare), encoding="utf-8")
    (work / f"{name}.gate.il").write_text(
        leclib.render(gate, set(gold.key_points), compare), encoding="utf-8")
    (work / f"{name}.ys").write_text(leclib.abc_script(
        top, f"{name}.gold.il", f"{name}.gate.il", f"{name}.aig"),
        encoding="utf-8")
    (work / f"{name}.abc").write_text(
        leclib.ABC_CEC.format(aig=f"{name}.aig"), encoding="utf-8")
    try:
        proc = checklib.run_tool(
            [str(EDA_BIN), "bash", "-c",
             f"yosys -q -l {name}.log -s {name}.ys"
             f" && yosys-abc -f {name}.abc > {name}.out 2>&1"],
            wall=ABC_TIMEOUT_S, cwd=str(work), capture_output=True, text=True,
            encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return False
    out = work / f"{name}.out"
    return proc.returncode == 0 and out.is_file() and leclib.abc_verdict(
        out.read_text(encoding="utf-8", errors="replace"))


def prove_constants(top: str, work: Path, gold: leclib.View,
                    gate: leclib.View) -> dict[str, int]:
    """Gold-only flops that hold a constant, proved, then tied to it.

    Synthesis drops a flop whose D gives back its own value. Every candidate
    starts at 0, then 1, then is not a constant; one sat run checks the whole
    set at once, the rest of the flops free, until it holds."""
    cands = sorted(k for k in set(gold.key_points) - set(gate.key_points)
                   if gold.key_points[k].kind == "ff"
                   and {"D", "Q"} <= set(gold.key_points[k].pins))
    guess = {k: 0 for k in cands}
    il = "constants.il"
    (work / il).write_text(leclib.render(gold, set(cands)), encoding="utf-8")
    for round_ in range(2 * len(cands) + 1):
        if not guess:
            break
        log = _yosys_sat(leclib.constants_script(
            il, top, guess, gold, MITER_TIMEOUT_S), f"constants{round_}", work)
        bad = leclib.constants_failed(log or "", guess)
        if bad is None:
            return {}
        if not bad:
            break
        for k in bad:
            if guess[k] == 0:
                guess[k] = 1
            else:
                del guess[k]
    else:
        return {}
    for k, c in guess.items():
        leclib.fix_constant(gold, k, c)
    return dict(sorted(guess.items()))


def run_link(link: str, top: str, work: Path, gold: leclib.View,
             gate: leclib.View, facts: dict) -> list[dict]:
    """One miter proves the link; eqy, one partition per point, then runs
    only on the points the miter found different, to name each one."""
    only_gold = sorted(set(gold.key_points) - set(gate.key_points))
    only_gate = sorted(set(gate.key_points) - set(gold.key_points))
    facts.update({"key_points": len(set(gold.key_points) & set(gate.key_points)),
                  "unmatched": {"gold": only_gold, "gate": only_gate}})
    every = (set(leclib.points(gold, set(gate.key_points)))
             & set(leclib.points(gate, set(gold.key_points))))
    # A proof of nothing is not a proof: both views must have reached the
    # comparison with something in it.
    if not every or not gold.outputs:
        raise CheckError(f"{link} compared nothing: no shared output or "
                         "key point")
    facts["compared"] = len(every)
    differ: dict[str, dict] = {}
    if abc_proves(top, work, gold, gate, every):
        facts["proved"] = len(every)
        facts["prover"] = "abc cec"
        return []
    facts["prover"] = "yosys sat"
    status = "different"
    for round_ in range(MITER_ROUNDS):
        status, found, cex = run_miter(top, work, gold, gate, every - set(differ),
                                       f"miter{round_}")
        if status != "different" or not found:
            break
        differ.update(cex)
        if not every - set(differ):
            status = "equal"
            break
    if status == "unknown":
        facts["proved"] = 0
        return [checklib.violation(
            "lec", "error", None, link, "lec_unproven", [],
            f"{link}: the miter did not finish ({len(every)} points): not "
            "proven", "yosys sat", link=link)]
    if status == "different":
        # More points differ than the rounds could name; they are not proved.
        facts["unnamed_failures"] = True
        facts["proved"] = 0
    else:
        facts["proved"] = len(every) - len(differ)
    return [_violation(link, gold, point, verdict, cex)
            for point, (verdict, cex) in name_failures(
                link, top, work, gold, gate, differ).items()]


def name_failures(link: str, top: str, work: Path, gold: leclib.View,
                  gate: leclib.View, differ: dict[str, dict]) -> dict:
    """{point: (eqy status, counterexample)} for the points the miter found
    different. eqy disagreeing with the miter doesn't clear a point: its
    counterexample stands."""
    found = {p: ("FAIL", differ[p]) for p in differ}
    named = sorted(differ)[:NAME_MAX]
    if not named:
        return found
    eqy = work / "eqy"
    eqy.mkdir(exist_ok=True)
    (eqy / "gold.il").write_text(
        leclib.render(gold, set(gate.key_points), set(named)), encoding="utf-8")
    (eqy / "gate.il").write_text(
        leclib.render(gate, set(gold.key_points), set(named)), encoding="utf-8")
    (eqy / "lec.eqy").write_text(
        leclib.eqy_config(top, "gold.il", "gate.il", PARTITION_TIMEOUT_S),
        encoding="utf-8")
    try:
        proc = _tool([str(EDA_BIN), "eqy", "-f", "-j", JOBS, "lec.eqy"], eqy)
    except subprocess.TimeoutExpired:
        return found
    listing = eqy / "lec" / "partition.list"
    if proc.returncode not in (0, 2) or not listing.is_file():
        return found
    by_part = {leclib.partition_name(p): p for p in named}
    for ln in listing.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        part = ln.split()[1]
        short = part.split(".", 1)[1] if "." in part else part
        point = by_part.get(short)
        if point is None:
            continue
        status_file = eqy / "lec" / "strategies" / part / "sat" / "status"
        status = (status_file.read_text(encoding="utf-8").strip()
                  if status_file.is_file() else "NOT RUN")
        log = eqy / "lec" / "strategies" / part / "sat" / "run.log"
        if status == "FAIL" and log.is_file():
            found[point] = ("FAIL", leclib.counterexample(log.read_text(
                encoding="utf-8", errors="replace")))
        else:
            found[point] = (status, differ[point])
    return found


def _violation(link: str, gold: leclib.View, point: str, eqy: str,
               cex: dict) -> dict:
    where = leclib.describe_point(point)
    kp = gold.key_points.get(where.get("key_point", ""))
    if kp is not None:
        where["q_net"] = kp.q_net()
    extra = {} if eqy == "FAIL" else {
        "note": f"eqy said {eqy} for this partition alone; the link's miter "
                "found the counterexample shown"}
    return checklib.violation(
        "lec", "error", None, point, "lec_not_equivalent", [],
        f"{link}: {_say(where, point)} differs", "yosys sat + eqy", link=link,
        counterexample=cex, **where, **extra)


def _say(where: dict, point: str) -> str:
    if "key_point" in where:
        net = where.get("q_net")
        named = where["key_point"] + (f" (Q {net})" if
                                      net and net != where["key_point"] else "")
        return f"{where['pin_kind']} of {named}"
    return f"output {point}"


def run_kepler(gold: Path, gate: Path, liberty: Path, work: Path) -> dict:
    try:
        proc = _tool([str(EDA_BIN), "kepler-formal", "-verilog", "-v", "lec",
                      str(gold), str(gate), str(liberty)], work)
    except subprocess.TimeoutExpired as exc:
        return {"tool": "kepler-formal", "verdict": "inconclusive",
                "reason": checklib.timeout_why(exc)}
    return {"tool": "kepler-formal", **leclib.kepler_verdict(
        (proc.stdout or "") + (proc.stderr or ""), proc.returncode)}


def rtl_reads(ws: Path, wrapper: Path) -> list[str]:
    """As LibreLane reads them: -sv for all, SYNTHESIS defined, so a block's
    `ifdef SYNTHESIS cells are the ones in the layout."""
    if not wrapper.is_file():
        raise CheckError(f"no {wrapper} - the harden gate has not run yet")
    files = [*check_synth.collect_sources(ws), wrapper]
    return [f"read_verilog -sv -DSYNTHESIS {f.resolve()}" for f in files]


def synth_view(run_dir: Path, wrapper: str) -> Path | None:
    hits = sorted(run_dir.glob(f"*-yosys-synthesis/{wrapper}.nl.v"))
    return hits[-1] if hits else None


def unmodelled_used(sources: list[Path], unmodelled: dict) -> dict:
    used = set()
    for src in sources:
        used |= leclib.cells_used(src.read_text(encoding="utf-8",
                                                errors="replace"))
    return {c: unmodelled[c] for c in sorted(used & set(unmodelled))}


def prove_link(link: str, reads_gold: list[str], reads_gate: list[str],
               top: str, liberty: Path, work_root: Path,
               fsms: list[leclib.Fsm] = ()) -> tuple[dict, list]:
    work = work_root / link
    work.mkdir(parents=True, exist_ok=True)
    # Only a netlist has instances to match on (see leclib's docstring).
    key_by = "net" if link.startswith("rtl_") else "instance"
    gold = write_view("gold", reads_gold, top, liberty, work, key_by)
    gate = write_view("gate", reads_gate, top, liberty, work, key_by)
    aliases = leclib.alias_table(
        (work / "gold.aliases.json").read_text(encoding="utf-8"), top)
    if key_by == "net":
        try:
            leclib.rekey_by_alias(gold, aliases, set(gate.key_points))
        except leclib.LecError as exc:
            raise CheckError(f"{link}: {exc}") from exc
    else:
        leclib.name_q_nets(gold, aliases)
    facts: dict = {"fsm": [leclib.apply_fsm(f, gold, gate, aliases)
                           for f in fsms]}
    facts["constants"] = prove_constants(top, work, gold, gate)
    violations = run_link(link, top, work, gold, gate, facts)
    facts["rtl_init"] = gold.has_init
    return facts, violations


def recoded(run_dir: Path) -> list[leclib.Fsm]:
    """The FSM maps LibreLane's synthesis wrote in its log, if it kept one."""
    text = "".join(p.read_text(encoding="utf-8", errors="replace")
                   for p in sorted(run_dir.glob("*-yosys-synthesis/*.log")))
    return leclib.recoded_fsms(text)


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)

    ws = Path(args.workspace).resolve()
    spec = speclib.load_spec(ws / "spec" / "spec.yaml")
    top = spec.get("top")
    if not isinstance(top, str) or not top.strip():
        raise CheckError("spec.yaml has no non-empty 'top' - run spec_lint first")
    wrapper = ttlib.wrapper_name(spec)
    run_dir = ws / "harden" / "runs" / "run"
    final = run_dir / "final" / "nl" / f"{wrapper}.nl.v"
    if not final.is_file():
        raise CheckError(f"no {final} - the harden gate has not run yet")
    liberty = check_synth.liberty_path(check_synth.toolchain_root(),
                                       check_synth.choose_std_cell(spec))
    if not liberty.is_file():
        raise CheckError(f"liberty file not found: {liberty}")
    _sequential, unmodelled = leclib.scan_liberty(
        liberty.read_text(encoding="utf-8", errors="replace"))

    work_root = ws / "log" / "lec"
    shutil.rmtree(work_root, ignore_errors=True)
    work_root.mkdir(parents=True, exist_ok=True)

    rtl = rtl_reads(ws, ws / "harden" / f"{wrapper}.v")
    synth = synth_view(run_dir, wrapper)
    if synth is None:
        raise CheckError(f"no {run_dir}/*-yosys-synthesis/{wrapper}.nl.v: the "
                         "run dir was trimmed. Re-run harden to get it back; "
                         "final/nl alone can't be matched to RTL")
    synth_read = [f"read_verilog {synth}"]
    plan = [("rtl_vs_synth", rtl, synth_read, recoded(run_dir)),
            ("synth_vs_final", synth_read, [f"read_verilog {final}"], [])]

    violations, links = [], {}
    sources = [*check_synth.collect_sources(ws), synth, final]
    for cell, why in unmodelled_used(sources, unmodelled).items():
        violations.append(checklib.violation(
            "lec", "error", None, cell, "lec_unmodelled_cell", [],
            f"{cell}: {why}: its liberty model has no function, so what it "
            "drives is not proved", "liberty"))
    if violations:
        return checklib.report(
            SCRIPT, ws / "harden", violations, top=top, links=links,
            cross_check={"tool": "kepler-formal", "verdict": "not_run"},
            not_checked=NOT_CHECKED), args.out
    for link, gold, gate, fsms in plan:
        facts, found = prove_link(link, gold, gate, wrapper, liberty, work_root,
                                  fsms)
        links[link] = facts
        violations.extend(found)

    work = work_root / "kepler"
    work.mkdir(parents=True, exist_ok=True)
    cross = run_kepler(synth, final, liberty, work)
    eqy_differs = any(v["kind"] == "lec_not_equivalent"
                      and v.get("link") == "synth_vs_final"
                      for v in violations)
    cross["eqy"] = "different" if eqy_differs else "equal"
    if cross["verdict"] == "different" and not eqy_differs:
        violations.append(checklib.violation(
            "lec", "error", None, "synth_vs_final", "lec_engines_disagree",
            [], "kepler-formal found a difference eqy proved absent: see "
            "log/lec/kepler/miter_log_*.txt", "kepler-formal"))

    payload = checklib.report(
        SCRIPT, ws / "harden", violations, top=top, links=links,
        cross_check=cross, not_checked=NOT_CHECKED)
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
