#!/usr/bin/env python
"""check_lint.py - the lint gate (docs/design.md 1.5, "### M2.").

    check_lint.py --workspace DIR [--out FILE]

Runs `verilator --lint-only -Wall -Wno-fatal --top-module <spec.yaml top>`
(bin/eda's own loader-wrapping dispatch, docs/design.md 1.2 - launched as a
subprocess of `bin/eda`, the same path tests/check.sh's own verilator-lint
smoke uses, never a bare host `verilator`) over every rtl/*.v and rtl/*.sv
file. `-Wno-fatal` matters here: without it verilator stops and exits
nonzero the moment ANY warning fires, which would collapse "no errors,
warnings only from an allowlist" into one bit; with it every warning is
printed and this script does its own severity classification against the
allowlist, using verilator's own exit code only to catch a real compile
error (a bad literal `%Error:` line with no rule, never allowlist-able).

Pass criteria (gates.yaml `lint` row): no errors; warnings only from an
allowlist with reasons. The allowlist is `rtl/lint_allow.yaml` (optional; a
list of `{rule, reason, file?, signal?, line?}`) - it lives inside rtl/ on purpose, not as a
separate artifact kind, so the existing `rtl` dir_text hash already covers
an allowlist edit (docs/design.md 1.6). A warning whose rule is not on the
list is reported at severity "error" (gates.yaml's fail_severities is
`[error]` for every M1-registered row, so an unlisted warning must be
elevated to fail, not merely surfaced at "warning"); an allowlisted warning
is downgraded to "info" (visible in the report, never counted). Fault this
gate must catch: "an always @* missing an else (latch)" - verilator's own
LATCH rule, live under plain -Wall, with no reason to allowlist it.

Waivers are narrow and go stale (#92a). An entry's optional `file` (the
finding's path, e.g. `rtl/top.v`, or just its basename), `signal` (a name
the warning quotes, e.g. verilator's "Signal is not used: 'x'") and `line`
narrow it to the findings that match all of them as well as `rule`. A
rule-only entry still works, but it waives that rule everywhere, so each
one is reported as a `lint_broad_waiver` finding at "info" (visible, never
counted). An entry that matched no finding at all is a `lint_stale_waiver`
error: the warning it was written for is gone (or the entry never matched
what it was meant to), and a stale waiver would silently cover the next,
unrelated warning of that rule. An unknown key (a typo such as `singal`)
is refused rather than read as a broader waiver.

Two more parsers read the same files after verilator (#91), because
verilator alone lets through SystemVerilog the rest of the flow cannot use:

- slang (`bin/eda slang --quiet --top <top>`), a strict IEEE 1800 front
  end. Its errors (a name declared twice in one scope, a variable driven
  from two always_ff blocks, an always_comb variable also assigned
  elsewhere) are findings of kind `slang_<name>` for slang's own
  `[-W<name>]` (`slang_redefinition`, `slang_multiple-always-assigns`;
  `slang_error` when it names none), severity error, source `slang`, never
  allowlist-able. Its warnings go through the same allowlist as
  verilator's, keyed by that same `slang_<name>` kind.
- iverilog (`bin/eda iverilog -g2012 -t null -s <top>`), the parser the sim
  gate compiles with (cocotb's Icarus runner, -g2012). Plenty of SV both
  verilator and slang accept is a "sorry: ... not supported" or a syntax
  error to it (`inside`, streaming operators, unpacked structs, `let`,
  concurrent assertions); without this pass such a design passed lint and
  first failed at sim, three gates later. Only a nonzero exit counts (it
  prints non-fatal "sorry" lines on designs it then compiles fine); each
  failing line is a finding of kind `sim_unsupported` (a "sorry") or
  `sim_compile_error`, severity error, source `iverilog`, and is never
  allowlist-able: sim cannot run that design at all.
- slang again, on the synthesis view (`-DSYNTHESIS`, as yosys defines it,
  so code under `ifdef SIM` or `ifndef SYNTHESIS` is out), dumping its
  elaborated AST (`--ast-json`) for four sim/synth mismatches that all
  three parsers above accept (#92b): `lint_incomplete_sensitivity` (an
  `always @(a)` that reads b: sim re-runs it on a only, synthesis builds
  @*), `lint_initial_block`, `lint_delay` (`#n`) and `lint_x_assign` (an x
  on an assignment's right side). Source `slang-synth`, severity error,
  allowlist-able like any warning (narrowly, by file and signal). A
  synthesis view that does not compile reports slang's errors with the
  same `slang_<name>` kinds. Blocking assignment in a clocked block and a
  latch from a case without default are verilator's BLKSEQ and LATCH.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
REPO = ENGINE.parent
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import speclib  # noqa: E402
from checklib import CheckError  # noqa: E402

import yaml  # noqa: E402

SCRIPT = "check_lint"
EDA_BIN = REPO / "bin" / "eda"
ALLOW_REL = "rtl/lint_allow.yaml"
# A wall ceiling only, ten times the old fixed limit: the tool is ended
# sooner when it stops making progress (checklib.STALL_S).
TIMEOUT_S = 600.0

# %Warning-RULE: file:line:col: msg   (a lint rule warning)
# %Error: file:line:col: msg          (a real compile error, never allowlist-able)
# %Error: Exiting due to N error(s)   (verilator's own summary - not a finding)
LINE_RE = re.compile(
    r"^%(?P<sev>Error|Warning)(?:-(?P<rule>[A-Z0-9_]+))?:\s*"
    r"(?:(?P<file>\S+?):(?P<line>\d+):(?P<col>\d+):\s*)?(?P<msg>.*)$")


ALLOW_KEYS = {"rule", "reason", "file", "signal", "line"}
QUOTED_RE = re.compile(r"'([^']+)'")


class Allowlist:
    """rtl/lint_allow.yaml's entries, each counting the findings it waived.

    `match` returns the waiving entry's reason (or None); `findings` then
    reports the broad (rule-only) entries at info and the ones that matched
    nothing at error. A plain `{rule: reason}` dict is accepted too (every
    entry rule-only) for callers and tests that build one by hand.
    """

    def __init__(self, entries=None):
        if isinstance(entries, dict):
            entries = [{"rule": r, "reason": why} for r, why in entries.items()]
        self.entries = [dict(e, hits=0) for e in (entries or [])]

    def __bool__(self):
        return bool(self.entries)

    @staticmethod
    def _file_ok(want, got):
        if want is None:
            return True
        if not got:
            return False
        want, got = want.replace("\\", "/"), got.replace("\\", "/")
        return got == want or got.endswith("/" + want.removeprefix("./")) \
            or Path(got).name == want

    @staticmethod
    def _signal_ok(want, msg):
        if want is None:
            return True
        for name in QUOTED_RE.findall(msg or ""):
            if name == want or name.rsplit(".", 1)[-1] == want:
                return True
        return False

    def match(self, kind, file_, line, msg):
        for e in self.entries:
            if e["rule"] != kind:
                continue
            if not self._file_ok(e.get("file"), file_):
                continue
            if not self._signal_ok(e.get("signal"), msg):
                continue
            if e.get("line") is not None and e["line"] != line:
                continue
            e["hits"] += 1
            return e["reason"]
        return None

    def findings(self):
        out = []
        for i, e in enumerate(self.entries):
            where = ", ".join(f"{k}={e[k]}" for k in ("file", "signal", "line")
                              if e.get(k) is not None)
            label = f"{ALLOW_REL}[{i}] rule {e['rule']}" + (
                f" ({where})" if where else "")
            if not e["hits"]:
                out.append(checklib.violation(
                    "lint", "error", ALLOW_REL, None, "lint_stale_waiver", [],
                    f"{label} matches no lint finding: the warning it waived "
                    "is gone (or the entry never matched it); delete or "
                    "narrow it", "check_lint", line=None))
            elif not where:
                out.append(checklib.violation(
                    "lint", "info", ALLOW_REL, None, "lint_broad_waiver", [],
                    f"{label} waives every {e['rule']} warning in the design "
                    f"({e['hits']} now); give it a file and signal (or line) "
                    "so a new one is not silently covered", "check_lint",
                    line=None))
        return out


def load_allowlist(ws: Path) -> Allowlist:
    p = ws / ALLOW_REL
    if not p.is_file():
        return Allowlist()
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    if not isinstance(data, list):
        raise CheckError(f"{p} must be a YAML list of {{rule, reason, file?, "
                         "signal?, line?}}")
    entries = []
    for i, entry in enumerate(data):
        if not isinstance(entry, dict) or not entry.get("rule") \
                or not entry.get("reason"):
            raise CheckError(f"{p}[{i}] must have a non-empty 'rule' and "
                             "'reason'")
        extra = set(entry) - ALLOW_KEYS
        if extra:
            raise CheckError(f"{p}[{i}] has unknown key(s) {sorted(extra)}; "
                             f"allowed: {sorted(ALLOW_KEYS)}")
        e = {"rule": str(entry["rule"]), "reason": str(entry["reason"])}
        for k in ("file", "signal"):
            if entry.get(k) is not None:
                if not isinstance(entry[k], str) or not entry[k].strip():
                    raise CheckError(f"{p}[{i}] '{k}' must be a non-empty "
                                     "string")
                e[k] = entry[k].strip()
        if entry.get("line") is not None:
            if isinstance(entry["line"], bool) or not isinstance(
                    entry["line"], int) or entry["line"] < 1:
                raise CheckError(f"{p}[{i}] 'line' must be a positive integer")
            e["line"] = entry["line"]
        entries.append(e)
    return Allowlist(entries)


def collect_sources(ws: Path) -> list[Path]:
    rtl_dir = ws / "rtl"
    files = sorted(rtl_dir.glob("*.v")) + sorted(rtl_dir.glob("*.sv"))
    if not files:
        raise CheckError(f"no .v/.sv files under {rtl_dir}")
    return checklib.order_sources(files)


def run_verilator(ws: Path, top: str, files: list[Path]) -> str:
    rel = [str(f.relative_to(ws)) for f in files]
    try:
        proc = checklib.run_tool(
            [str(EDA_BIN), "verilator", "--lint-only", "-Wall", "-Wno-fatal",
             "--top-module", top, *rel],
            cwd=str(ws), capture_output=True, text=True,
            encoding="utf-8", errors="replace", wall=TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"verilator --lint-only "
                         f"{checklib.timeout_why(exc)}: {exc}") from exc
    output = (proc.stdout or "") + (proc.stderr or "")
    # A real verilator error still prints %Error lines even under
    # -Wno-fatal (parse_violations below reports those as findings); a
    # launcher that never reached verilator at all (bin/eda misconfigured,
    # a missing binary, exit 127) exits nonzero too but leaves no %Error to
    # explain it - that used to fall straight through to parse_violations,
    # which found nothing to parse and reported zero violations: a launcher
    # failure passing lint clean. Caught here instead, before parsing.
    if proc.returncode != 0 and "%Error" not in output:
        raise CheckError(
            f"verilator --lint-only exited {proc.returncode} with no "
            f"%Error line to explain it (the launcher likely never reached "
            f"verilator): {output[-2000:]}")
    return output


# file:line:col: error|warning: message [-Wname]   (slang, --quiet)
SLANG_RE = re.compile(
    r"^(?P<file>[^\s:][^:]*):(?P<line>\d+):(?P<col>\d+): "
    r"(?P<sev>error|warning): (?P<msg>.*?)(?: \[(?P<rule>-W[\w-]+)\])?$")
# file:line: [error: |sorry: |syntax error] message   (iverilog)
IVL_RE = re.compile(
    r"^(?P<file>[^\s:][^:]*):(?P<line>\d+):\s*(?P<msg>(?P<kind>sorry|error|"
    r"syntax error)\b.*)$")


def _rel(ws: Path, files: list[Path]) -> list[str]:
    return [str(f.relative_to(ws)) for f in files]


def _run_eda(ws: Path, args: list[str], what: str):
    try:
        proc = checklib.run_tool(
            [str(EDA_BIN), *args], cwd=str(ws), capture_output=True,
            text=True, encoding="utf-8", errors="replace", wall=TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"{what} {checklib.timeout_why(exc)}: {exc}") from exc
    return proc, (proc.stdout or "") + (proc.stderr or "")


def run_slang(ws: Path, top: str, files: list[Path],
              allow) -> list[dict]:
    if not isinstance(allow, Allowlist):
        allow = Allowlist(allow)
    proc, output = _run_eda(
        ws, ["slang", "--quiet", "--top", top, *_rel(ws, files)], "slang")
    violations = []
    for line in output.splitlines():
        m = SLANG_RE.match(line.rstrip())
        if not m:
            continue  # the echoed source line, the caret line, a note
        rule = m.group("rule")
        kind = ("slang_" + rule[2:] if rule
                else f"slang_{m.group('sev')}")
        if m.group("sev") == "error":
            sev_out, reason = "error", None
        else:
            reason = allow.match(kind, m.group("file"),
                                 int(m.group("line")), m.group("msg"))
            sev_out = "info" if reason is not None else "error"
        extras = {"reason": reason} if reason is not None else {}
        violations.append(checklib.violation(
            "lint", sev_out, m.group("file"), None, kind, [],
            m.group("msg").strip(), "slang", line=int(m.group("line")),
            **extras))
    if proc.returncode != 0 and not any(
            v["severity"] == "error" for v in violations):
        raise CheckError(
            f"slang exited {proc.returncode} with no error line to explain "
            f"it (the launcher likely never reached slang): {output[-2000:]}")
    return violations


# #92b: rules for code that simulates one way and synthesizes another, read
# from slang's elaborated AST of the synthesis view (-DSYNTHESIS, which
# yosys defines too, so `ifdef SIM` and `ifndef SYNTHESIS` code is out).
SYNTH_RULES = {
    "lint_incomplete_sensitivity":
        "an always block with a sensitivity list that leaves out a signal "
        "it reads; synthesis builds it as @*, so sim and netlist differ",
    "lint_initial_block":
        "an initial block in synthesized RTL; synthesis drops it, so the "
        "netlist powers up with whatever the sim never saw",
    "lint_delay":
        "a #delay in synthesized RTL; synthesis ignores it",
    "lint_x_assign":
        "an x assigned in synthesized RTL; sim propagates x where the "
        "netlist picks 0 or 1",
}
SIGNAL_KINDS = frozenset({"Net", "Variable"})


def _sym(ref):
    """'5716874311624 a' -> ('5716874311624', 'a')."""
    addr, _, name = str(ref).partition(" ")
    return addr, name


def _walk(node):
    if isinstance(node, dict):
        if node.get("isUninstantiated"):
            return  # a generate branch not taken: never synthesized
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _reads(node, kinds, out):
    """Signals an expression or statement reads (an assignment's target
    is not a read, but its index expressions are)."""
    if isinstance(node, list):
        for v in node:
            _reads(v, kinds, out)
        return
    if not isinstance(node, dict):
        return
    k = node.get("kind")
    if k == "Assignment":
        _target_reads(node.get("left"), kinds, out)
        _reads(node.get("right"), kinds, out)
        return
    if k == "NamedValue":
        addr, name = _sym(node.get("symbol"))
        if kinds.get(addr) in SIGNAL_KINDS:
            out[addr] = name
        return
    for v in node.values():
        _reads(v, kinds, out)


def _target_reads(node, kinds, out):
    if not isinstance(node, dict):
        return
    k = node.get("kind")
    if k == "NamedValue":
        return
    if k in ("ElementSelect", "RangeSelect", "MemberAccess"):
        _target_reads(node.get("value"), kinds, out)
        for key in ("selector", "left", "right"):
            _reads(node.get(key), kinds, out)
        return
    if k == "Concatenation":
        for op in node.get("operands") or []:
            _target_reads(op, kinds, out)
        return
    _reads(node, kinds, out)


def _targets(node):
    """Names an assignment subtree writes, with their symbol addrs."""
    out = {}
    for n in _walk(node):
        if n.get("kind") != "Assignment":
            continue
        stack = [n.get("left")]
        while stack:
            t = stack.pop()
            if not isinstance(t, dict):
                continue
            if t.get("kind") == "NamedValue":
                addr, name = _sym(t.get("symbol"))
                out[addr] = name
            elif t.get("kind") == "Concatenation":
                stack.extend(t.get("operands") or [])
            else:
                stack.append(t.get("value"))
    return out


def _is_x(lit):
    v = str(lit.get("value", ""))
    if "'" not in v:
        return False
    tail = v.split("'", 1)[1].lstrip("sS")
    if tail[:1] in ("b", "B", "o", "O", "d", "D", "h", "H"):
        tail = tail[1:]
    return "x" in tail.lower()


def _loc(node):
    return (node.get("source_file") or node.get("source_file_start"),
            node.get("source_line") or node.get("source_line_start"))


def _quoted(names):
    return ", ".join(f"'{n}'" for n in sorted(set(names)))


def synth_rule_findings(ast: dict) -> list[tuple]:
    """(rule, file, line, msg) for each sim/synth mismatch in the AST."""
    kinds = {}
    for n in _walk(ast):
        if "addr" in n and "kind" in n:
            kinds[str(n["addr"])] = n["kind"]
    found = []
    for n in _walk(ast):
        k = n.get("kind")
        if k == "ProceduralBlock" and n.get("procedureKind") == "Initial":
            f, ln = _loc(n)
            names = list(_targets(n.get("body")).values())
            found.append(("lint_initial_block", f, ln,
                          "initial block" + (f" assigns {_quoted(names)}"
                                             if names else "") +
                          " in the synthesis view: synthesis drops it; reset "
                          "the value instead, or put it under `ifdef SIM`"))
        elif k == "ProceduralBlock" and n.get("procedureKind") == "Always":
            body = n.get("body") or {}
            if body.get("kind") != "Timed":
                continue
            timing = body.get("timing") or {}
            events = (timing.get("events") if timing.get("kind") == "EventList"
                      else [timing])
            if not events or any(
                    not isinstance(e, dict) or e.get("kind") != "SignalEvent"
                    or e.get("edge") != "None" for e in events):
                continue  # @*, clocked, or a mix: not a plain comb list
            listed = {}
            for e in events:
                _reads(e.get("expr"), kinds, listed)
            read = {}
            _reads(body.get("stmt"), kinds, read)
            written = _targets(body.get("stmt"))
            missing = [name for addr, name in read.items()
                       if addr not in listed and addr not in written]
            if missing:
                f, ln = _loc(n)
                found.append(("lint_incomplete_sensitivity", f, ln,
                              f"always block reads {_quoted(missing)} but "
                              "its sensitivity list leaves it out: sim "
                              "re-runs it only on the listed signals, while "
                              "synthesis builds it as @*; use always @* "
                              "(or always_comb)"))
        elif k in ("Delay", "Delay3"):
            f, ln = _loc(n)
            found.append(("lint_delay", f, ln,
                          "#delay in the synthesis view: synthesis ignores "
                          "it, so sim timing is not the netlist's; drop it "
                          "or put it under `ifdef SIM`"))
        elif k in ("Assignment",):
            if any(x.get("kind") in ("IntegerLiteral",
                                     "UnbasedUnsizedIntegerLiteral")
                   and _is_x(x) for x in _walk(n.get("right"))):
                f, ln = _loc(n)
                names = list(_targets(n).values())
                found.append(("lint_x_assign", f, ln,
                              f"assigns x to {_quoted(names)} in the "
                              "synthesis view: sim propagates x, while "
                              "synthesis picks 0 or 1 as it likes; assign "
                              "a real value"))
    seen, out = set(), []
    for item in found:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def run_synth_rules(ws: Path, top: str, files: list[Path],
                    allow) -> list[dict]:
    if not isinstance(allow, Allowlist):
        allow = Allowlist(allow)
    try:
        proc = checklib.run_tool(
            [str(EDA_BIN), "slang", "--quiet", "--top", top, "-DSYNTHESIS",
             "--ast-json", "-", "--ast-json-source-info", *_rel(ws, files)],
            cwd=str(ws), capture_output=True, text=True, encoding="utf-8",
            errors="replace", wall=TIMEOUT_S)
    except subprocess.TimeoutExpired as exc:
        raise CheckError(f"slang --ast-json {checklib.timeout_why(exc)}: "
                         f"{exc}") from exc
    violations = []
    if proc.returncode != 0:
        # the synthesis view does not compile (code under `ifdef
        # SYNTHESIS` the plain view never saw): its errors are findings.
        for line in (proc.stderr or "").splitlines() + \
                (proc.stdout or "").splitlines():
            m = SLANG_RE.match(line.rstrip())
            if m and m.group("sev") == "error":
                rule = m.group("rule")
                violations.append(checklib.violation(
                    "lint", "error", m.group("file"), None,
                    "slang_" + rule[2:] if rule else "slang_error", [],
                    "with SYNTHESIS defined: " + m.group("msg").strip(),
                    "slang-synth", line=int(m.group("line"))))
        if not violations:
            raise CheckError(
                f"slang --ast-json exited {proc.returncode} with no error "
                f"line to explain it: {(proc.stderr or '')[-2000:]}")
        return violations
    try:
        import json
        ast = json.loads(proc.stdout or "")
    except ValueError as exc:
        raise CheckError(f"slang --ast-json printed no JSON ({exc}): "
                         f"{(proc.stdout or '')[:500]}") from exc
    for rule, file_, line, msg in synth_rule_findings(ast):
        reason = allow.match(rule, file_, line, msg)
        extras = {"reason": reason} if reason is not None else {}
        violations.append(checklib.violation(
            "lint", "info" if reason is not None else "error", file_, None,
            rule, [], msg, "slang-synth", line=line, **extras))
    return violations


def run_iverilog(ws: Path, top: str, files: list[Path]) -> list[dict]:
    proc, output = _run_eda(
        ws, ["iverilog", "-g2012", "-t", "null", "-s", top,
             *_rel(ws, files)], "iverilog")
    if proc.returncode == 0:
        return []  # a "sorry" it compiled past anyway is not a finding
    violations = []
    for line in output.splitlines():
        m = IVL_RE.match(line.rstrip())
        if not m:
            continue
        kind = ("sim_unsupported" if m.group("kind") == "sorry"
                else "sim_compile_error")
        violations.append(checklib.violation(
            "lint", "error", m.group("file"), None, kind, [],
            "iverilog (the sim gate's compiler) rejects this: "
            + m.group("msg").strip(), "iverilog", line=int(m.group("line"))))
    if not violations:
        raise CheckError(
            f"iverilog exited {proc.returncode} with no file:line error to "
            f"explain it (the launcher likely never reached iverilog): "
            f"{output[-2000:]}")
    return violations


def parse_violations(output: str, allow) -> list[dict]:
    if not isinstance(allow, Allowlist):
        allow = Allowlist(allow)
    violations = []
    for line in output.splitlines():
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        msg = m.group("msg").strip()
        if msg.startswith("Exiting due to"):
            continue  # verilator's own summary line, not a finding
        sev, rule = m.group("sev"), m.group("rule")
        file_ = m.group("file")
        line_no = int(m.group("line")) if m.group("line") else None
        if sev == "Error":
            violations.append(checklib.violation(
                "lint", "error", file_, None, rule or "compile_error", [],
                msg, "verilator", line=line_no))
            continue
        # Warning: allowlisted -> info (visible, never counted); otherwise
        # elevated to error (fail_severities: [error] is what gate.py counts).
        reason = allow.match(rule, file_, line_no, msg) if rule else None
        sev_out = "info" if reason is not None else "error"
        extras = {"reason": reason} if reason is not None else {}
        violations.append(checklib.violation(
            "lint", sev_out, file_, None, rule or "warning", [], msg,
            "verilator", line=line_no, **extras))
    return violations


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)

    ws = Path(args.workspace)
    spec = speclib.load_spec(ws / "spec" / "spec.yaml")
    top = spec.get("top")
    if not isinstance(top, str) or not top.strip():
        raise CheckError("spec.yaml has no non-empty 'top' - run spec_lint "
                         "first")
    files = collect_sources(ws)
    allow = load_allowlist(ws)
    output = run_verilator(ws, top, files)
    violations = parse_violations(output, allow)
    violations += run_slang(ws, top, files, allow)
    violations += run_synth_rules(ws, top, files, allow)
    violations += run_iverilog(ws, top, files)
    violations += allow.findings()
    # stamp() hashes exactly this path as input_digest; gate.py's own
    # record_gate cross-checks that against invalidation.yaml's gate_inputs
    # kinds[0] for this gate ("rtl" for lint) - the whole workspace would
    # never match that and silently fail every real recording.
    payload = checklib.report(SCRIPT, ws / "rtl", violations, top=top,
                              files=[str(f.relative_to(ws)) for f in files])
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
