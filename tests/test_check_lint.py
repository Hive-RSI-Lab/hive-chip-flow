"""engine/scripts/check_lint.py: the lint gate (docs/design.md 1.5's lint
row). Runs the REAL verilator through bin/eda - not hermetic, needs the eda
image - same as tests/check.sh's own verilator-lint smoke."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
SCRIPTS = ENGINE / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))

import check_lint  # noqa: E402
import gate  # noqa: E402
import yaml as _yaml  # noqa: E402

GATES_YAML = ENGINE / "reference" / "gates.yaml"


def _lint_gate_row():
    rows = _yaml.safe_load(GATES_YAML.read_text(encoding="utf-8"))["gates"]["vde"]
    return rows["lint"]

CLEAN_V = """\
module top (input wire clk, input wire rst, output reg [3:0] count);
  always @(posedge clk) count <= rst ? 4'd0 : count + 4'd1;
endmodule
"""

LATCH_V = """\
module top (input wire sel, input wire [1:0] a, output reg [1:0] q);
  always @(*) begin
    if (sel)
      q = a;
  end
endmodule
"""

WIDTH_V = """\
module top (input wire [7:0] a, output reg [3:0] q);
  always @(*) q = a;
endmodule
"""


# #91: legal SV that verilator and slang both accept but iverilog - the sim
# gate's compiler - refuses ("sorry: \"inside\" expressions not supported").
INSIDE_SV = """\
module top (input logic [3:0] a, output logic b);
  assign b = a inside {4'd1, 4'd3, 4'd5};
endmodule
"""

# #91: two always_ff driving one variable - verilator only warns
# (MULTIDRIVEN, allowlist-able); slang's -Wmultiple-always-assigns is an
# error no allowlist can silence.
TWOFF_SV = """\
module top (input logic clk, input logic a, output logic b);
  always_ff @(posedge clk) b <= a;
  always_ff @(posedge clk) b <= ~a;
endmodule
"""


def make_ws(tmp_path: Path, rtl_text: str, top: str = "top",
            name: str = "top.v") -> Path:
    ws = tmp_path / "ws"
    (ws / "rtl").mkdir(parents=True)
    (ws / "spec").mkdir(parents=True)
    (ws / "rtl" / name).write_text(rtl_text, encoding="utf-8")
    (ws / "spec" / "spec.yaml").write_text(
        f"top: {top}\nrequirements: []\n", encoding="utf-8")
    return ws


@pytest.mark.slow
def test_clean_design_passes(tmp_path, capsys):
    ws = make_ws(tmp_path, CLEAN_V)
    code = check_lint.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["status"] == "pass"
    assert out["violations"] == []


@pytest.mark.slow
def test_latch_fails_uncovered_else(tmp_path, capsys):
    # gates.yaml's named fault for `lint`: "an always @* missing an else
    # (latch)".
    ws = make_ws(tmp_path, LATCH_V)
    code = check_lint.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    kinds = {v["kind"] for v in out["violations"]}
    assert "LATCH" in kinds
    v = next(v for v in out["violations"] if v["kind"] == "LATCH")
    assert v["severity"] == "error"
    assert v["source"] == "verilator"


@pytest.mark.slow
def test_unallowlisted_warning_fails(tmp_path, capsys):
    ws = make_ws(tmp_path, WIDTH_V)
    code = check_lint.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    kinds = {v["kind"]: v["severity"] for v in out["violations"]}
    assert kinds.get("WIDTHTRUNC") == "error"


@pytest.mark.slow
def test_allowlisted_warning_downgrades_to_info_and_gate_passes(tmp_path, capsys):
    # check_<gate>.py's own exit reflects "found anything at all" (docs/
    # design.md 1.1: exit 1 = findings); it is gate.py's evaluate(), applying
    # gates.yaml's fail_severities/max_count, that turns an all-"info"
    # violations list into a passing GATE - exactly test_evaluate_pass_and_
    # fail_thresholds in test_gate.py for the warning-severity case.
    ws = make_ws(tmp_path, WIDTH_V)
    (ws / "rtl" / "lint_allow.yaml").write_text(
        "- rule: WIDTHTRUNC\n  reason: intentional narrowing, top 4 bits "
        "of a are a status field read elsewhere\n"
        "- rule: UNUSEDSIGNAL\n  reason: the unused high bits are reserved\n",
        encoding="utf-8")
    code = check_lint.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out          # script level: it still found something
    severities = {v["severity"] for v in out["violations"]}
    assert severities == {"info"}
    reasons = {v["kind"]: v.get("reason") for v in out["violations"]}
    assert reasons["WIDTHTRUNC"]
    # #92a: both entries are rule-only, so each is reported as broad
    broad = [v for v in out["violations"] if v["kind"] == "lint_broad_waiver"]
    assert len(broad) == 2, out

    gate_result = gate.evaluate("lint", _lint_gate_row(), out)
    assert gate_result["status"] == "pass"   # gate level: no "error" severity


def test_missing_spec_yaml_is_an_error(tmp_path, capsys):
    ws = tmp_path / "ws"
    (ws / "rtl").mkdir(parents=True)
    (ws / "rtl" / "top.v").write_text(CLEAN_V, encoding="utf-8")
    code = check_lint.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert out["status"] == "error"
    assert out["remediation"]


def test_launcher_failure_with_no_percent_lines_is_an_error(
        tmp_path, capsys, monkeypatch):
    # a launcher that exits nonzero without ever reaching verilator (a bad
    # bin/eda, EDA_TOOLCHAIN pointed nowhere, exit 127) used to leave
    # parse_violations nothing to parse -> zero violations -> a clean pass.
    ws = make_ws(tmp_path, CLEAN_V)
    bad_eda = tmp_path / "bad-eda.sh"
    bad_eda.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    bad_eda.chmod(0o755)
    monkeypatch.setattr(check_lint, "EDA_BIN", bad_eda)
    code = check_lint.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2, out
    assert out["status"] == "error"
    assert "127" in out["remediation"]


def test_no_rtl_files_is_an_error(tmp_path, capsys):
    ws = tmp_path / "ws"
    (ws / "rtl").mkdir(parents=True)
    (ws / "spec").mkdir(parents=True)
    (ws / "spec" / "spec.yaml").write_text(
        "top: top\nrequirements: []\n", encoding="utf-8")
    code = check_lint.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert "no .v/.sv files" in out["remediation"]


def _lint(ws, capsys):
    code = check_lint.main(["--workspace", str(ws)])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.slow
def test_sv_iverilog_rejects_fails_at_lint_naming_the_construct(
        tmp_path, capsys):
    ws = make_ws(tmp_path, INSIDE_SV, name="top.sv")
    code, out = _lint(ws, capsys)
    assert code == 1, out
    hits = [v for v in out["violations"] if v["source"] == "iverilog"]
    assert hits and hits[0]["kind"] == "sim_unsupported", out
    assert hits[0]["severity"] == "error"
    assert "inside" in hits[0]["msg"]
    assert gate.evaluate("lint", _lint_gate_row(), out)["status"] == "fail"


@pytest.mark.slow
def test_slang_error_survives_a_verilator_allowlist(tmp_path, capsys):
    ws = make_ws(tmp_path, TWOFF_SV, name="top.sv")
    (ws / "rtl" / "lint_allow.yaml").write_text(
        "- rule: MULTIDRIVEN\n  reason: test - verilator's warning only\n"
        "- rule: slang_multiple-always-assigns\n  reason: errors are never "
        "allowlist-able\n", encoding="utf-8")
    code, out = _lint(ws, capsys)
    sev = {(v["source"], v["kind"]): v["severity"] for v in out["violations"]}
    assert sev[("verilator", "MULTIDRIVEN")] == "info", out
    assert sev[("slang", "slang_multiple-always-assigns")] == "error", out
    assert gate.evaluate("lint", _lint_gate_row(), out)["status"] == "fail"


def test_slang_and_iverilog_line_parsing(tmp_path, monkeypatch):
    """Hermetic: the two new parsers read their tools' real line formats
    (captured from the eda image) and skip echoed source and caret lines."""
    import subprocess as sp
    outputs = {
        "slang": (1, "rtl/top.sv:3:9: error: redefinition of 't' "
                     "[-Wredefinition]\n  logic t;\n        ^\n"
                     "rtl/top.sv:2:9: note: previous definition here\n"
                     "rtl/top.sv:4:3: warning: unused [-Wunused-net]\n"),
        "iverilog": (2, "rtl/top.sv:2: sorry: \"inside\" expressions not "
                        "supported yet.\nrtl/top.sv:3:      : It was "
                        "declared here as a variable.\n"),
    }

    def fake(cmd, wall, **kw):
        rc, text = outputs[cmd[1]]
        return sp.CompletedProcess(cmd, rc, text, "")
    monkeypatch.setattr(check_lint.checklib, "run_tool", fake)
    ws = tmp_path
    files = [ws / "rtl" / "top.sv"]
    sl = check_lint.run_slang(ws, "top", files,
                              {"slang_unused-net": "reserved"})
    assert [(v["kind"], v["severity"], v["line"]) for v in sl] == [
        ("slang_redefinition", "error", 3),
        ("slang_unused-net", "info", 4)]
    iv = check_lint.run_iverilog(ws, "top", files)
    assert [(v["kind"], v["line"]) for v in iv] == [("sim_unsupported", 2)]
    assert all(v["source"] == "iverilog" for v in iv)


def test_iverilog_nonzero_with_nothing_parsed_is_an_error(
        tmp_path, monkeypatch):
    import subprocess as sp
    monkeypatch.setattr(
        check_lint.checklib, "run_tool",
        lambda cmd, wall, **kw: sp.CompletedProcess(cmd, 127, "", "nope"))
    with pytest.raises(check_lint.CheckError, match="127"):
        check_lint.run_iverilog(tmp_path, "top", [tmp_path / "rtl/top.sv"])
    with pytest.raises(check_lint.CheckError, match="127"):
        check_lint.run_slang(tmp_path, "top", [tmp_path / "rtl/top.sv"], {})


@pytest.mark.slow
@pytest.mark.parametrize("design", [
    "corpus/vde/counter8", "corpus/vde/riscv", "corpus/vde/spi_fifo",
    "corpus/vde/uart", "corpus/msde/dac_tile/digital",
    "corpus/msde/sensor_counted/digital", "tests/fixtures/sv_fsm",
    "blocks/pll/digital"])
def test_corpus_and_sv_fsm_clean_under_slang_and_iverilog(
        tmp_path, capsys, design):
    """#91's second and third parsers add no finding to any shipped design
    (sensor_counted keeps its one verilator SYNCASYNCNET, as before)."""
    import shutil
    src = REPO / design
    ws = tmp_path / "ws"
    shutil.copytree(src / "rtl", ws / "rtl")
    (ws / "spec").mkdir()
    spec = src / "spec.yaml" if (src / "spec.yaml").is_file() \
        else src / "spec" / "spec.yaml"
    shutil.copy(spec, ws / "spec" / "spec.yaml")
    _, out = _lint(ws, capsys)
    assert out["status"] in ("pass", "violations"), out
    extra = [v for v in out["violations"]
             if v["source"] in ("slang", "iverilog")
             or v["source"].startswith("slang-") and v["severity"] != "info"]
    assert extra == [], extra
    if design == "blocks/pll/digital":
        # #92c: three views of the PLL (sim, synth with hand-placed cells,
        # formal) build the same ports and flip-flops; the one formal-only
        # output is the harness's observation point, reported as info.
        ifdef = [(v["kind"], v["severity"]) for v in out["violations"]
                 if v["source"] == "slang-ifdef"]
        assert ifdef == [("lint_ifdef_formal_port", "info")], out
        assert gate.evaluate("lint", _lint_gate_row(), out)["status"] == \
            "pass"


# #92a: two unused signals; a waiver naming one must not cover the other.
TWO_UNUSED_V = """\
module top (input wire clk, input wire [3:0] spare_a, input wire [3:0] spare_b,
            output reg [3:0] q);
  always @(posedge clk) q <= 4'd0;
endmodule
"""


@pytest.mark.slow
def test_narrow_waiver_covers_one_signal_and_stale_one_fails(tmp_path, capsys):
    ws = make_ws(tmp_path, TWO_UNUSED_V)
    allow = ws / "rtl" / "lint_allow.yaml"
    allow.write_text(
        "- rule: UNUSEDSIGNAL\n  file: rtl/top.v\n  signal: spare_a\n"
        "  reason: reserved input, tied off at the next level\n",
        encoding="utf-8")
    code, out = _lint(ws, capsys)
    unused = {}
    for v in out["violations"]:
        if v["kind"] == "UNUSEDSIGNAL":
            name = "spare_a" if "spare_a" in v["msg"] else (
                "spare_b" if "spare_b" in v["msg"] else v["msg"])
            unused[name] = v["severity"]
    assert unused == {"spare_a": "info", "spare_b": "error"}, out
    kinds = {v["kind"] for v in out["violations"]}
    assert not kinds & {"lint_broad_waiver", "lint_stale_waiver"}, out
    assert gate.evaluate("lint", _lint_gate_row(), out)["status"] == "fail"

    # waive both by name: the gate passes
    allow.write_text(allow.read_text(encoding="utf-8") +
                     "- rule: UNUSEDSIGNAL\n  signal: spare_b\n  reason: "
                     "reserved too\n", encoding="utf-8")
    code, out = _lint(ws, capsys)
    assert gate.evaluate("lint", _lint_gate_row(), out)["status"] == "pass", out

    # a waiver that matches nothing (a signal that no longer exists) fails
    allow.write_text(allow.read_text(encoding="utf-8") +
                     "- rule: UNUSEDSIGNAL\n  signal: spare_gone\n  reason: "
                     "was reserved\n", encoding="utf-8")
    code, out = _lint(ws, capsys)
    stale = [v for v in out["violations"] if v["kind"] == "lint_stale_waiver"]
    assert len(stale) == 1 and stale[0]["severity"] == "error", out
    assert "spare_gone" in stale[0]["msg"]
    assert stale[0]["file"] == "rtl/lint_allow.yaml"
    assert gate.evaluate("lint", _lint_gate_row(), out)["status"] == "fail"


def test_waiver_matching_broad_and_stale_hermetic(tmp_path):
    """Hermetic: file/signal/line narrow a waiver, a rule-only entry is
    reported as broad (info), an unmatched one as stale (error), and an
    unknown key or a bad line is refused."""
    out = ("%Warning-UNUSEDSIGNAL: rtl/top.v:2:10: Signal is not used: 'a'\n"
           "%Warning-UNUSEDSIGNAL: rtl/top.v:3:10: Signal is not used: 'b'\n"
           "%Warning-WIDTHTRUNC: rtl/sub.v:7:5: Operator ASSIGN expects 4 "
           "bits\n")
    al = check_lint.Allowlist([
        {"rule": "UNUSEDSIGNAL", "reason": "r", "file": "top.v",
         "signal": "a"},
        {"rule": "WIDTHTRUNC", "reason": "r", "file": "rtl/sub.v", "line": 7},
        {"rule": "WIDTHTRUNC", "reason": "r", "line": 99},
        {"rule": "LATCH", "reason": "r"},
    ])
    vs = check_lint.parse_violations(out, al)
    assert [(v["kind"], v["severity"]) for v in vs] == [
        ("UNUSEDSIGNAL", "info"), ("UNUSEDSIGNAL", "error"),
        ("WIDTHTRUNC", "info")]
    extra = [(v["kind"], v["severity"]) for v in al.findings()]
    assert extra == [("lint_stale_waiver", "error"),
                     ("lint_stale_waiver", "error")]
    al2 = check_lint.Allowlist({"UNUSEDSIGNAL": "r"})
    check_lint.parse_violations(out, al2)
    assert [(v["kind"], v["severity"]) for v in al2.findings()] == [
        ("lint_broad_waiver", "info")]

    ws = make_ws(tmp_path, CLEAN_V)
    p = ws / "rtl" / "lint_allow.yaml"
    for bad in ("- rule: X\n  reason: r\n  singal: a\n",
                "- rule: X\n  reason: r\n  line: zero\n",
                "- rule: X\n  reason: r\n  signal: ''\n"):
        p.write_text(bad, encoding="utf-8")
        with pytest.raises(check_lint.CheckError):
            check_lint.load_allowlist(ws)


# #92b: issue 92's probe - four sim/synth mismatches that verilator -Wall,
# slang and iverilog all pass - plus the same constructs where synthesis
# never sees them (`ifdef SIM`, `ifndef SYNTHESIS`) and the clean forms.
MISMATCH_V = """\
module top (input wire clk, input wire a, input wire b, output reg y,
            output reg q, output wire z, output reg c, output reg d);
  always @(a) y = a & b;
  initial q = 1'b0;
  always @(posedge clk) q <= #1 ~q;
  assign z = 1'bx;
  always @(*) c = a & b;
  always @(a or b) begin : t
    reg tmp;
    tmp = a;
    d = tmp | b;
  end
`ifdef SIM
  initial $display("sim only");
`endif
`ifndef SYNTHESIS
  initial #5 $display("not for synthesis");
`endif
endmodule
"""


@pytest.mark.slow
def test_sim_synth_mismatches_each_fail_with_their_own_rule(tmp_path, capsys):
    ws = make_ws(tmp_path, MISMATCH_V)
    code, out = _lint(ws, capsys)
    got = sorted((v["kind"], v["line"]) for v in out["violations"]
                 if v["source"] == "slang-synth")
    assert got == [("lint_delay", 5), ("lint_incomplete_sensitivity", 3),
                   ("lint_initial_block", 4), ("lint_x_assign", 6)], out
    assert all(v["severity"] == "error" for v in out["violations"]
               if v["source"] == "slang-synth")
    sens = next(v for v in out["violations"]
                if v["kind"] == "lint_incomplete_sensitivity")
    assert "'b'" in sens["msg"] and "'a'" not in sens["msg"]
    assert gate.evaluate("lint", _lint_gate_row(), out)["status"] == "fail"

    # a narrow waiver covers its one finding and nothing else
    (ws / "rtl" / "lint_allow.yaml").write_text(
        "- rule: lint_x_assign\n  file: rtl/top.v\n  signal: z\n"
        "  reason: test - a don't-care the netlist may pick\n",
        encoding="utf-8")
    code, out = _lint(ws, capsys)
    sev = {v["kind"]: v["severity"] for v in out["violations"]
           if v["source"] == "slang-synth"}
    assert sev["lint_x_assign"] == "info"
    assert sev["lint_initial_block"] == "error"


def test_synth_rules_hermetic_edges():
    """Hermetic: an uninstantiated generate branch is skipped, a clocked or
    @* block is never 'incomplete', and a signal written in the block
    before it is read is not missing from the list."""
    def nv(addr, name):
        return {"kind": "NamedValue", "symbol": f"{addr} {name}"}

    def asg(lhs, rhs, line):
        return {"kind": "Assignment", "left": lhs, "right": rhs,
                "source_line_start": line, "source_file_start": "rtl/t.v"}

    def always(events, stmt, line):
        return {"kind": "ProceduralBlock", "procedureKind": "Always",
                "source_file": "rtl/t.v", "source_line": line,
                "body": {"kind": "Timed", "timing": events, "stmt": stmt}}
    sym = [{"kind": "Variable", "addr": i, "name": n}
           for i, n in ((1, "a"), (2, "b"), (3, "y"), (4, "t"))]
    ev_a = {"kind": "SignalEvent", "edge": "None", "expr": nv(1, "a")}
    clk = {"kind": "SignalEvent", "edge": "PosEdge", "expr": nv(1, "a")}
    ast = {"members": sym + [
        always(ev_a, {"kind": "List", "list": [
            asg(nv(4, "t"), nv(1, "a"), 10),
            asg(nv(3, "y"), {"kind": "BinaryOp", "left": nv(4, "t"),
                             "right": nv(2, "b")}, 11)]}, 10),
        always(clk, asg(nv(3, "y"), nv(2, "b"), 20), 20),
        always({"kind": "ImplicitEvent"}, asg(nv(3, "y"), nv(2, "b"), 30),
               30),
        {"kind": "GenerateBlock", "isUninstantiated": True, "members": [
            {"kind": "ProceduralBlock", "procedureKind": "Initial",
             "source_line": 40, "body": asg(nv(3, "y"), nv(1, "a"), 40)}]},
    ]}
    got = check_lint.synth_rule_findings(ast)
    assert [(r, ln) for r, _, ln, _ in got] == [
        ("lint_incomplete_sensitivity", 10)]
    assert "'b'" in got[0][3] and "'t'" not in got[0][3]
    assert check_lint._is_x({"value": "4'b10x1"})
    assert check_lint._is_x({"value": "'x"})
    assert not check_lint._is_x({"value": "8'hff"})
    assert not check_lint._is_x({"value": "4'bzzzz"})


# #92c: the same file read with each define set the flow uses (none for sim
# and lint, SYNTHESIS for yosys, FORMAL on top of it for formal).
DIVERGE_V = """\
module top (input wire clk, input wire a, output reg q
`ifdef SYNTHESIS
  , output wire dbg
`endif
`ifdef FORMAL
  , output wire seen, input wire f_in
`endif
);
  always @(posedge clk) q <= a;
`ifndef SYNTHESIS
  reg shadow;
  always @(posedge clk) shadow <= q;
`endif
`ifdef SYNTHESIS
  assign dbg = q;
`endif
`ifdef FORMAL
  assign seen = q ^ f_in;
`endif
  sub u_sub (.d(a));
endmodule

module sub (
`ifdef SYNTHESIS
  input wire [1:0] d
`else
  input wire [3:0] d
`endif
);
endmodule
"""


@pytest.mark.slow
def test_ifdef_divergence_fails_each_port_and_flop(tmp_path, capsys):
    ws = make_ws(tmp_path, DIVERGE_V)
    code, out = _lint(ws, capsys)
    got = sorted((v["kind"], v["line"], v["severity"])
                 for v in out["violations"] if v["source"] == "slang-ifdef")
    assert got == [
        ("lint_ifdef_divergence", 3, "error"),    # dbg: synth+formal only
        ("lint_ifdef_divergence", 6, "error"),    # f_in: a formal-only input
        ("lint_ifdef_divergence", 12, "error"),   # shadow: sim-only flop
        ("lint_ifdef_divergence", 27, "error"),   # sub.d: width by define
        ("lint_ifdef_formal_port", 6, "info"),    # seen: formal's own output
    ], out
    msgs = {v["msg"].split("'")[1] for v in out["violations"]
            if v["source"] == "slang-ifdef"}
    assert msgs == {"dbg", "f_in", "shadow", "seen", "u_sub.d"}, msgs
    assert gate.evaluate("lint", _lint_gate_row(), out)["status"] == "fail"

    # a narrow waiver covers its one flop and nothing else
    (ws / "rtl" / "lint_allow.yaml").write_text(
        "- rule: lint_ifdef_divergence\n  file: top.v\n  signal: shadow\n"
        "  reason: test - a sim-only checker register\n"
        "- rule: UNUSEDSIGNAL\n  file: top.v\n  signal: shadow\n"
        "  reason: test - read by nobody in the plain view\n",
        encoding="utf-8")
    code, out = _lint(ws, capsys)
    sev = {v["msg"].split("'")[1]: v["severity"] for v in out["violations"]
           if v["source"] == "slang-ifdef"}
    assert sev["shadow"] == "info" and sev["dbg"] == "error", sev


def test_ifdef_divergence_hermetic(tmp_path, monkeypatch):
    """Hermetic: no SYNTHESIS/FORMAL branch means no extra elaboration; a
    FORMAL view that does not compile reports its errors (and only those,
    since the plain and SYNTHESIS views report their own elsewhere)."""
    ws = tmp_path
    (ws / "rtl").mkdir()
    f = ws / "rtl" / "t.v"
    f.write_text("module t; `ifdef SIM\n`endif\nendmodule\n")

    def boom(*a, **k):
        raise AssertionError("elaborated a design with no such branch")
    monkeypatch.setattr(check_lint, "_slang_ast", boom)
    assert check_lint.run_ifdef_divergence(ws, "t", [f], {}) == []

    f.write_text("module t;\n  `ifdef FORMAL\n  `endif\nendmodule\n")
    calls = []

    def fake(ws_, top, files, defines, label):
        calls.append(label)
        if label == "FORMAL":
            return None, [{"kind": "slang_undeclared", "label": label}]
        return {"members": []}, []
    monkeypatch.setattr(check_lint, "_slang_ast", fake)
    got = check_lint.run_ifdef_divergence(ws, "t", [f], {})
    assert calls == ["no define", "SYNTHESIS", "FORMAL"]
    assert got == [{"kind": "slang_undeclared", "label": "FORMAL"}]
    assert check_lint.FLOP_CELL_RE.match("dffrnq_2")
    assert check_lint.FLOP_CELL_RE.match("dfxtp_1")
    assert check_lint.FLOP_CELL_RE.match("sdffq_1")
    assert check_lint.FLOP_CELL_RE.match("dlxtp_1")
    assert not check_lint.FLOP_CELL_RE.match("dlya_1")
    assert not check_lint.FLOP_CELL_RE.match("clkbuf_8")
