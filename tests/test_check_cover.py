"""engine/scripts/check_cover.py: the cover gate (docs/design.md 1.5's
`cover` row, "### M3."). Runs the REAL cocotb tests over the VERILATOR
simulator (--coverage) through the eda image - not hermetic, needs the eda
image, and the slowest gate in this file (a verilator build per test) so
kept to a small design."""
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

import check_cover  # noqa: E402
import gate  # noqa: E402
import yaml as _yaml  # noqa: E402

GATES_YAML = ENGINE / "reference" / "gates.yaml"


def _cover_gate_row():
    rows = _yaml.safe_load(GATES_YAML.read_text(encoding="utf-8"))["gates"]["vde"]
    return rows["cover"]


RTL = """\
module top (input wire clk, input wire rst, output reg [3:0] count);
  always @(posedge clk) count <= rst ? 4'd0 : count + 4'd1;
endmodule
"""

# gates.yaml's own named fault for `cover`: "an unreachable state" - the
# extra branch's guard contradicts itself, so no test (however thorough)
# can ever reach it.
RTL_UNREACHABLE = """\
module top (input wire clk, input wire rst, output reg [3:0] count);
  always @(posedge clk) begin
    if (rst)
      count <= 4'd0;
    else if (rst && !rst)
      count <= 4'hA;
    else
      count <= count + 4'd1;
  end
endmodule
"""

TB = """\
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge

@cocotb.test()
async def test_counts_every_cycle(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    await FallingEdge(dut.clk)
    dut.rst.value = 1
    await FallingEdge(dut.clk)
    dut.rst.value = 0
    prev = int(dut.count.value)
    for _ in range(20):
        await FallingEdge(dut.clk)
        cur = int(dut.count.value)
        assert cur == (prev + 1) % 16
        prev = cur
"""


def make_ws(tmp_path: Path, rtl_text: str) -> Path:
    ws = tmp_path / "ws"
    (ws / "rtl").mkdir(parents=True)
    (ws / "spec").mkdir(parents=True)
    (ws / "tb").mkdir(parents=True)
    (ws / "log").mkdir(parents=True)
    (ws / "rtl" / "top.v").write_text(rtl_text, encoding="utf-8")
    (ws / "spec" / "spec.yaml").write_text(
        "top: top\nrequirements: []\n", encoding="utf-8")
    (ws / "tb" / "test_top.py").write_text(TB, encoding="utf-8")
    return ws


@pytest.mark.slow
def test_full_coverage_passes(tmp_path, capsys):
    ws = make_ws(tmp_path, RTL)
    code = check_cover.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["status"] == "pass"
    assert out["line_pct"] == 100.0
    assert out["toggle_pct"] == 100.0
    # simdiff (#86): both simulators ran the one test and agree
    sd = out["simdiff"]
    assert sd["agree"] is True
    assert {s: r["result"] for s, r in
            sd["tests"]["test_counts_every_cycle"].items()} == {
        "icarus": "pass", "verilator": "pass"}
    assert all(sd["runs"][s]["wall_s"] > 0 and sd["runs"][s]["cpu_s"] > 0
               for s in ("icarus", "verilator"))
    # coverage.dat must never linger in tb/ - it would flip tb's own
    # dir_text hash (invalidation.yaml) for sim/mutate/holdout too.
    assert not (ws / "tb" / "coverage.dat").exists()


@pytest.mark.slow
def test_unreachable_state_fails_the_threshold(tmp_path, capsys):
    ws = make_ws(tmp_path, RTL_UNREACHABLE)
    code = check_cover.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    errors = {v["kind"] for v in out["violations"] if v["severity"] == "error"}
    assert "line_coverage_below_threshold" in errors
    infos = {v["kind"] for v in out["violations"] if v["severity"] == "info"}
    assert "line_not_covered" in infos
    gate_result = gate.evaluate("cover", _cover_gate_row(), out)
    assert gate_result["status"] == "fail"


def test_exclusion_removes_a_point_from_the_denominator():
    # count_coverage() is the aggregation/threshold math on its own - pure,
    # no real verilator+cocotb build needed (tests/check.sh's own two-
    # minute budget, docs/design.md 1.1, is why this is not a third real
    # end-to-end run like the two above).
    per_file = {"top.v": {"line": {5: 3, 6: 0, 7: 3}, "toggle": {}}}
    violations, counts = check_cover.count_coverage(
        per_file, {"top.v"}, {"line": {}, "toggle": {}}, 95.0, 90.0)
    assert counts["line_pct"] < 95.0
    assert any(v["kind"] == "line_coverage_below_threshold" for v in violations)

    excludes = {"line": {("top.v", 6): "guard is intentionally dead, kept "
                         "for a future revision"}, "toggle": {}}
    violations2, counts2 = check_cover.count_coverage(
        per_file, {"top.v"}, excludes, 95.0, 90.0)
    assert counts2["line_pct"] == 100.0
    assert counts2["line_total"] == 2  # the excluded line drops out entirely
    assert violations2 == []


# #80: a state machine whose tests enter every state and flip every bit,
# but never take the RUN->IDLE abort - line and toggle are both 100%.
RTL_FSM_SKIPPED_ARC = """\
module top (input wire clk, input wire rst, input wire go, input wire stop,
            output reg [1:0] state);
  localparam IDLE = 2'd0, RUN = 2'd1, DONE = 2'd2, HOLD = 2'd3;
  always @(posedge clk) begin
    if (rst) state <= IDLE;
    else case (state)
      IDLE: state <= go ? RUN : IDLE;
      RUN: state <= stop ? IDLE : DONE;
      DONE: state <= HOLD;
      HOLD: state <= IDLE;
    endcase
  end
endmodule
"""

TB_FSM = """\
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge

@cocotb.test()
async def test_walks_every_state(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.go.value = 0
    dut.stop.value = 1
    dut.rst.value = 1
    await FallingEdge(dut.clk)
    dut.rst.value = 0
    await FallingEdge(dut.clk)
    for _ in range(3):
        dut.go.value = 1
        await FallingEdge(dut.clk)       # IDLE -> RUN (stop drops below)
        dut.go.value = 0
        dut.stop.value = 0
        await FallingEdge(dut.clk)       # RUN -> DONE
        dut.stop.value = 1              # stop toggles, but only in DONE/HOLD/IDLE
        await FallingEdge(dut.clk)       # DONE -> HOLD
        await FallingEdge(dut.clk)       # HOLD -> IDLE
        await FallingEdge(dut.clk)       # IDLE -> IDLE
        assert int(dut.state.value) == 0
"""

# #80: `a && b` evaluated only while en is high, and b is held high
# whenever en is - b toggles, the line runs, yet b never decides y.
RTL_EXPR_TERM = """\
module top (input wire clk, input wire en, input wire a, input wire b,
            output reg y);
  always @(posedge clk) if (en) y <= a && b;
endmodule
"""

TB_EXPR = """\
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge

@cocotb.test()
async def test_and_gate(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    for en, a, b in [(0, 0, 0), (1, 0, 1), (1, 1, 1), (0, 1, 0), (1, 0, 1),
                     (1, 1, 1), (0, 0, 0)]:
        dut.en.value, dut.a.value, dut.b.value = en, a, b
        await FallingEdge(dut.clk)
        if en:
            assert int(dut.y.value) == (a & b)
"""


def _errors_and_infos(out):
    return ({v["kind"]: v for v in out["violations"] if v["severity"] == "error"},
            [v for v in out["violations"] if v["severity"] == "info"])


@pytest.mark.slow
def test_fsm_skipped_transition_fails_naming_it(tmp_path, capsys):
    ws = make_ws(tmp_path, RTL_FSM_SKIPPED_ARC)
    (ws / "tb" / "test_top.py").write_text(TB_FSM, encoding="utf-8")
    code = check_cover.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    # line and toggle alone would have passed this suite
    assert out["line_pct"] == 100.0 and out["toggle_pct"] == 100.0
    errors, infos = _errors_and_infos(out)
    assert set(errors) == {"fsm_coverage_below_threshold"}, errors
    assert "top.state::RUN->IDLE" in errors["fsm_coverage_below_threshold"]["msg"]
    arcs = [v["label"] for v in infos if v["kind"] == "fsm_arc_not_covered"]
    assert arcs == ["top.state::RUN->IDLE"]
    assert not any(v["kind"] == "fsm_state_not_covered" for v in infos)
    assert gate.evaluate("cover", _cover_gate_row(), out)["status"] == "fail"

    # the owner's reasoned exclusion of that one arc passes the gate
    (ws / "rtl" / "cover_exclude.yaml").write_text(
        "- file: top.v\n  line: 6\n  label: top.state::RUN->IDLE\n"
        "  reason: the stop arc is covered by the system test, not this block's\n",
        encoding="utf-8")
    code = check_cover.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["fsm_pct"] == 100.0


@pytest.mark.slow
def test_expr_term_never_deciding_fails_naming_it(tmp_path, capsys):
    ws = make_ws(tmp_path, RTL_EXPR_TERM)
    (ws / "tb" / "test_top.py").write_text(TB_EXPR, encoding="utf-8")
    (ws / "spec" / "spec.yaml").write_text(
        "top: top\nrequirements: []\ncover: {expr_min: 100}\n",
        encoding="utf-8")
    code = check_cover.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    assert out["line_pct"] == 100.0 and out["toggle_pct"] == 100.0
    errors, infos = _errors_and_infos(out)
    assert set(errors) == {"expr_coverage_below_threshold"}, errors
    assert [v["label"] for v in infos if v["kind"] == "expr_not_covered"] \
        == ["(b==0) => 0"]
    assert out["fsm_total"] == 0 and out["fsm_pct"] is None


def _dat_line(count, **keys):
    body = "".join(f"\x01{k}\x02{v}" for k, v in keys.items())
    return f"C '{body}' {count}\n"


def test_parse_dat_reads_fsm_and_expr_points_only(tmp_path):
    dat = tmp_path / "coverage.dat"
    dat.write_text(
        "# SystemC::Coverage-3\n"
        + _dat_line(4, f="rtl/top.v", l=6, t="fsm_arc", o="top.s::ANY->A[reset]",
                    Fv="top.s", Ff="ANY", Ft="A", Fg="reset")
        + _dat_line(0, f="rtl/top.v", l=6, t="fsm_arc", o="top.s::A->B",
                    Fv="top.s", Ff="A", Ft="B")
        + _dat_line(2, f="rtl/top.v", l=6, t="fsm_state", o="top.s::A",
                    Fv="top.s", Ft="A")
        + _dat_line(3, f="rtl/top.v", l=9, t="expr", o="(a==1 && b==1) => 1")
        + _dat_line(7, f="rtl/top.v", l=9, t="branch", o="if")
        + _dat_line(7, f="rtl/top.v", l=9, t="toggle", o="a:0->1"),
        encoding="utf-8")
    pts = check_cover.parse_dat(dat)
    assert [(p["kind"], p["label"], p["count"]) for p in pts] == [
        ("fsm_arc", "top.s::ANY->A[reset]", 4), ("fsm_arc", "top.s::A->B", 0),
        ("fsm_state", "top.s::A", 2), ("expr", "(a==1 && b==1) => 1", 3)]
    assert pts[0]["reset"] and not pts[1]["reset"]
    assert (pts[1]["var"], pts[1]["from"], pts[1]["to"]) == ("top.s", "A", "B")


def _pt(kind, label, count, line=6, **fsm):
    return {"kind": kind, "file": "/ws/rtl/top.v", "line": line,
            "label": label, "count": count, "var": fsm.get("var"),
            "from": fsm.get("frm"), "to": fsm.get("to"),
            "reset": fsm.get("reset", False)}


NO_EXCLUDES = {"line": {}, "toggle": {}}


def test_count_fsm_expr_thresholds_and_exclusions():
    pts = [_pt("fsm_state", "top.s::A", 3, var="top.s", to="A"),
           _pt("fsm_state", "top.s::B", 1, var="top.s", to="B"),
           _pt("fsm_arc", "top.s::ANY->A[reset]", 1, var="top.s", frm="ANY",
               to="A", reset=True),
           _pt("fsm_arc", "top.s::A->B", 1, var="top.s", frm="A", to="B"),
           _pt("fsm_arc", "top.s::B->A", 0, var="top.s", frm="B", to="A"),
           _pt("expr", "(a==0) => 0", 5, line=9),
           _pt("expr", "(b==0) => 0", 0, line=9),
           _pt("fsm_state", "top.x::Q", 0, var="top.x", to="Q") | {
               "file": "/ws/tb/helper.v"}]  # not the DUT's rtl: ignored
    v, c = check_cover.count_fsm_expr(pts, {"top.v"}, NO_EXCLUDES, 100.0, 90.0)
    assert (c["fsm_covered"], c["fsm_total"]) == (4, 5)
    assert (c["expr_covered"], c["expr_total"]) == (1, 2)
    kinds = sorted(x["kind"] for x in v)
    assert kinds == ["expr_coverage_below_threshold", "expr_not_covered",
                     "fsm_arc_not_covered", "fsm_coverage_below_threshold"]
    fsm_err = next(x for x in v if x["kind"] == "fsm_coverage_below_threshold")
    assert fsm_err["missed"] == ["top.v:6 top.s::B->A"]

    ex = {"line": {}, "toggle": {("top.v", 6, "top.s::B->A"): "why",
                                 ("top.v", 9, "(b==0) => 0"): "why"}}
    v, c = check_cover.count_fsm_expr(pts, {"top.v"}, ex, 100.0, 90.0)
    assert v == [] and c["fsm_pct"] == 100.0 and c["expr_pct"] == 100.0


def test_count_fsm_expr_with_none_has_no_threshold():
    v, c = check_cover.count_fsm_expr([], {"top.v"}, NO_EXCLUDES, 100.0, 90.0)
    assert v == []
    assert c == {"fsm_pct": None, "fsm_covered": 0, "fsm_total": 0,
                 "expr_pct": None, "expr_covered": 0, "expr_total": 0}


def test_state_with_no_extracted_transition_is_named():
    # Verilator saw states A and B but no arc out of B (an else-if chain):
    # a count it never made must not read as coverage
    pts = [_pt("fsm_state", "top.s::A", 1, var="top.s", to="A"),
           _pt("fsm_state", "top.s::B", 1, var="top.s", to="B", line=8),
           _pt("fsm_arc", "top.s::ANY->A[reset]", 1, var="top.s", frm="ANY",
               to="A", reset=True),
           _pt("fsm_arc", "top.s::A->B", 1, var="top.s", frm="A", to="B")]
    v, _ = check_cover.count_fsm_expr(pts, {"top.v"}, NO_EXCLUDES, 100.0, 90.0)
    assert [(x["kind"], x["severity"], x["states"]) for x in v] == [
        ("fsm_arcs_not_seen", "info", ["B"])]


def test_new_cover_kinds_route_to_testbench_with_a_remediation():
    import cluster_violations
    rem = REPO / "skills" / "vde" / "reference" / "remediations"
    for kind in ("fsm_coverage_below_threshold", "expr_coverage_below_threshold",
                 "fsm_arc_not_covered", "fsm_state_not_covered",
                 "expr_not_covered", "fsm_arcs_not_seen"):
        assert cluster_violations.FIXER_HINTS[kind] == "testbench", kind
        assert (rem / f"{kind}.md").is_file(), kind


RTL_SYNTAX_ERROR = """\
module top (input wire clk, input wire rst, output reg [3:0] count);
  always @(posedge clk) count <= rst ? 4'd0 : count + 4'd1
  // missing semicolon above - a real verilator build error, not just a
  // lint warning.
endmodule
"""


@pytest.mark.slow
def test_failed_verilator_build_refuses_cover(tmp_path, capsys):
    # a build that never produces a coverage binary at all (a genuine
    # compile error, gates.yaml-adjacent to the launcher-crash cases
    # elsewhere in this file) must refuse the gate, never silently report
    # zero/partial coverage.
    ws = make_ws(tmp_path, RTL_SYNTAX_ERROR)
    code = check_cover.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2, out
    assert out["status"] == "error"
    assert "coverage run failed" in out["remediation"]
    assert not (ws / "tb" / "coverage.dat").exists()


def test_crashed_coverage_info_launcher_is_an_error(tmp_path, monkeypatch):
    # a failed launcher (nonzero exit, no output) fails the gate - unit-
    # level (no real cocotb+verilator build needed): write_coverage_info()
    # is the one piece of run_coverage() that shells out through bin/eda.
    from checklib import CheckError
    import pytest
    bad_eda = tmp_path / "bad-eda.sh"
    bad_eda.write_text("#!/bin/sh\nexit 127\n", encoding="utf-8")
    bad_eda.chmod(0o755)
    monkeypatch.setattr(check_cover, "EDA_BIN", bad_eda)
    fake_dat = tmp_path / "coverage.dat"
    fake_dat.write_text("# SystemC::Coverage-3\n", encoding="utf-8")
    with pytest.raises(CheckError, match="127"):
        check_cover.write_coverage_info(tmp_path, fake_dat)


def test_no_tb_modules_is_an_error(tmp_path, capsys):
    ws = make_ws(tmp_path, RTL)
    (ws / "tb" / "test_top.py").unlink()
    code = check_cover.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert "no test_*.py modules" in out["remediation"]


def test_bad_exclusion_entry_is_an_error(tmp_path, capsys):
    ws = make_ws(tmp_path, RTL)
    (ws / "rtl" / "cover_exclude.yaml").write_text(
        "- file: top.v\n  line: 6\n", encoding="utf-8")  # no reason
    code = check_cover.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2
    assert "reason" in out["remediation"]


def test_parse_info_distinguishes_toggle_from_branch_labels(tmp_path):
    info = tmp_path / "c.info"
    info.write_text(
        "SF:top.v\n"
        "DA:6,3\n"
        "BRDA:6,0,clk:0->1,3\n"
        "BRDA:7,0,if,1\n"
        "BRDA:7,0,else,0\n"
        "end_of_record\n", encoding="utf-8")
    parsed = check_cover.parse_info(info)
    data = parsed["top.v"]
    assert data["line"] == {6: 3}
    assert data["toggle"] == {(6, "clk:0->1"): 3}  # the "if"/"else" branch
    # labels never match TOGGLE_LABEL_RE and are correctly left out.


def test_parse_info_empty_report_is_an_error(tmp_path):
    from checklib import CheckError
    import pytest
    info = tmp_path / "empty.info"
    info.write_text("", encoding="utf-8")
    with pytest.raises(CheckError):
        check_cover.parse_info(info)


@pytest.mark.slow
def test_relative_workspace_reaches_full_coverage(tmp_path, capsys,
                                                   monkeypatch):
    """M5 regression (found running the real /vde skill end to end on
    counter8): write_coverage_info's own `cwd=str(ws)` combined with an
    unresolved-relative `info_path`/`coverage_dat` (both built from that
    same, possibly-relative, ws) doubled the workspace's own relative
    prefix under verilator_coverage's cwd and it could never find its own
    input. A relative --workspace must still reach full coverage."""
    ws = make_ws(tmp_path, RTL)
    monkeypatch.chdir(tmp_path)
    code = check_cover.main(["--workspace", "ws"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["status"] == "pass"
    assert out["line_pct"] == 100.0


# simdiff (#86): a blocking write `a` read by a block on a buffered copy of
# the clock. Icarus runs the copy-clocked block a delta later, so b sees the
# new a; Verilator runs it in source order, so b sees the old one.
RTL_RACE = """\
module top (input wire clk, input wire rst, output reg [3:0] count,
            output reg [3:0] prev);
  wire clk_b = clk;
  always @(posedge clk_b) if (rst) prev = 4'd0; else prev = count;
  always @(posedge clk) if (rst) count = 4'd0; else count = count + 4'd1;
endmodule
"""

TB_RACE = """\
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge

@cocotb.test()
async def test_prev_lags_count(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.rst.value = 1
    await FallingEdge(dut.clk)
    dut.rst.value = 0
    for _ in range(20):
        await FallingEdge(dut.clk)
        assert int(dut.prev.value) == (int(dut.count.value) - 1) % 16
"""


def _res(passed, skipped=False, wall=0.5):
    return {"passed": passed, "skipped": skipped, "wall_s": wall}


def test_simdiff_agreeing_runs_have_no_findings():
    vs, table = check_cover.simdiff({
        "icarus": {"t_a": _res(True), "t_b": _res(False)},
        "verilator": {"t_a": _res(True, wall=0.2), "t_b": _res(False)}})
    assert vs == []
    assert table["t_a"] == {"icarus": {"result": "pass", "wall_s": 0.5},
                            "verilator": {"result": "pass", "wall_s": 0.2}}
    assert table["t_b"]["icarus"]["result"] == "fail"


def test_simdiff_names_each_test_that_differs():
    vs, _ = check_cover.simdiff({
        "icarus": {"t_a": _res(True), "t_b": _res(False), "t_c": _res(True)},
        "verilator": {"t_a": _res(True), "t_b": _res(True)}})
    assert [(v["kind"], v["module"], v["severity"]) for v in vs] == [
        ("sim_mismatch", "t_b", "error"), ("sim_mismatch", "t_c", "error")]
    assert vs[0]["results"] == {"icarus": "fail", "verilator": "pass"}
    assert vs[1]["results"] == {"icarus": "pass", "verilator": "missing"}
    assert "t_b" in vs[0]["msg"]


def test_simdiff_skipped_against_pass_is_a_mismatch():
    vs, _ = check_cover.simdiff({"icarus": {"t": _res(False, True)},
                                 "verilator": {"t": _res(True)}})
    assert [v["kind"] for v in vs] == ["sim_mismatch"]


def test_simdiff_with_no_tests_is_an_error():
    with pytest.raises(check_cover.CheckError):
        check_cover.simdiff({"icarus": {}, "verilator": {}})


def test_sim_mismatch_routes_to_rtl_and_has_a_remediation():
    import cluster_violations
    assert cluster_violations.fixer_for("sim_mismatch") == "rtl"
    assert (REPO / "skills" / "vde" / "reference" / "remediations"
            / "sim_mismatch.md").is_file()


@pytest.mark.slow
def test_blocking_race_is_a_sim_mismatch_naming_the_test(tmp_path, capsys):
    ws = make_ws(tmp_path, RTL_RACE)
    (ws / "tb" / "test_top.py").write_text(TB_RACE, encoding="utf-8")
    code = check_cover.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    errs = [v for v in out["violations"] if v["severity"] == "error"]
    assert [(v["kind"], v["module"]) for v in errs] == [
        ("sim_mismatch", "test_prev_lags_count")], out
    assert errs[0]["results"] == {"icarus": "fail", "verilator": "pass"}
    assert out["simdiff"]["agree"] is False
