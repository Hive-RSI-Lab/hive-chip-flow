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
become ports, and eqy proves each key-point pin and output. Cell functions come
from the same liberty as timing. Fill, tap, decap and antenna cells have no
output so they vanish; buffers and delay cells are their function.

kepler-formal is the second engine on the netlist link. It compares D, CLK and
reset cones by instance name, so it can't see a flop's type: it passes a flop
swapped for its negedge or set twin, which eqy here fails. Agreement is
therefore one-sided: kepler finding a difference eqy proved absent is a
finding, and kepler seeing none where eqy failed is reported, not a finding.
kepler refusing to compare (renamed instance, cloned flop) is reported too.

Findings:

  lec_not_equivalent    a key-point pin or output differs, with a counterexample
  lec_unproven          eqy could not decide one (timeout, unknown). Not a pass.
  lec_unmodelled_cell   a used cell has no function in liberty (clock gate,
                        tri-state). No model, no proof: the gate stops there,
                        with `links` empty, and has proved nothing.
  lec_engines_disagree  kepler-formal found a difference eqy proved absent

Out of scope, in the report's `not_checked`: power-up values (`initial` and
init attributes; `rtl_init` says if the RTL has any), timing, and sequential
equivalence: retiming or a recoded FSM moves state, so names no longer match
and it fails rather than passing. `unmatched` lists key points in one view
only: those pass only if nothing observable reads them (a duplicate flop the
synthesiser merged). So a flop synthesis removed as constant fails if read.
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
JOBS = "2"
NOT_CHECKED = ["power-up values (no initial state exists in silicon)",
               "timing", "retiming and FSM recoding (they fail, not pass)"]


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


def run_eqy(link: str, top: str, work: Path, gold: leclib.View,
            gate: leclib.View) -> tuple[dict, list[dict]]:
    """Facts and violations for one link's proof."""
    (work / "gold.il").write_text(
        leclib.render(gold, set(gate.key_points)), encoding="utf-8")
    (work / "gate.il").write_text(
        leclib.render(gate, set(gold.key_points)), encoding="utf-8")
    (work / "lec.eqy").write_text(
        leclib.eqy_config(top, "gold.il", "gate.il", PARTITION_TIMEOUT_S),
        encoding="utf-8")
    only_gold = sorted(set(gold.key_points) - set(gate.key_points))
    only_gate = sorted(set(gate.key_points) - set(gold.key_points))
    facts = {"key_points": len(set(gold.key_points) & set(gate.key_points)),
             "unmatched": {"gold": only_gold, "gate": only_gate}}
    try:
        proc = _tool([str(EDA_BIN), "eqy", "-f", "-j", JOBS, "lec.eqy"], work)
    except subprocess.TimeoutExpired as exc:
        facts["proved"] = 0
        return facts, [checklib.violation(
            "lec", "error", None, link, "lec_unproven", [],
            f"{link}: eqy {checklib.timeout_why(exc)}: not proven", "eqy",
            link=link)]
    out = (proc.stdout or "") + (proc.stderr or "")
    listing = work / "lec" / "partition.list"
    if proc.returncode not in (0, 2) or not listing.is_file():
        raise CheckError(f"eqy on {link} exited {proc.returncode}: {out[-1500:]}")
    names = {}
    for port in list(gold.outputs):
        names[leclib.partition_name(port[1:])] = port[1:]
    shared = set(gold.key_points) & set(gate.key_points)
    for key in shared:
        for pin in gold.key_points[key].pins:
            if pin != "Q":
                point = f"{key}.{pin}"
                names[leclib.partition_name(point)] = point
    proved, violations = 0, []
    partitions = [ln.split()[1] for ln in
                  listing.read_text(encoding="utf-8").splitlines() if ln.strip()]
    for part in partitions:
        status_file = work / "lec" / "strategies" / part / "sat" / "status"
        status = (status_file.read_text(encoding="utf-8").strip()
                  if status_file.is_file() else "NOT RUN")
        if status == "PASS":
            proved += 1
            continue
        short = part.split(".", 1)[1] if "." in part else part
        point = names.get(short, short)
        where = leclib.describe_point(point)
        if "key_point" in where:
            where["q_net"] = gold.key_points[where["key_point"]].q_net()
        if status == "FAIL":
            log = work / "lec" / "strategies" / part / "sat" / "run.log"
            cex = (leclib.counterexample(log.read_text(
                encoding="utf-8", errors="replace"))
                if log.is_file() else {})
            violations.append(checklib.violation(
                "lec", "error", None, point, "lec_not_equivalent", [],
                f"{link}: {_say(where, point)} differs", "eqy", link=link,
                counterexample=cex, **where))
        else:
            violations.append(checklib.violation(
                "lec", "error", None, point, "lec_unproven", [],
                f"{link}: {_say(where, point)} not proven ({status})", "eqy",
                link=link, **where))
    facts["proved"] = proved
    facts["partitions"] = len(partitions)
    # A proof of nothing is not a proof: both views must have reached the
    # comparison with something in it.
    if not partitions or len(gold.outputs) == 0:
        raise CheckError(f"eqy on {link} compared nothing: {out[-800:]}")
    return facts, violations


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
               top: str, liberty: Path, work_root: Path) -> tuple[dict, list]:
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
    facts, violations = run_eqy(link, top, work, gold, gate)
    facts["rtl_init"] = gold.has_init
    return facts, violations


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
    plan = [("rtl_vs_synth", rtl, synth_read),
            ("synth_vs_final", synth_read, [f"read_verilog {final}"])]

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
    for link, gold, gate in plan:
        facts, found = prove_link(link, gold, gate, wrapper, liberty, work_root)
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
