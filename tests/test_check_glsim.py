"""engine/scripts/check_glsim.py: the glsim gate (docs/design.md 1.5's
glsim row, "### M4."). Fakes cocotb's own runner (never real Icarus/PDK
gate-level models - tests/smoke-harden.sh runs the real thing) to prove the
gate's failure classification and its "every test passes both ways" rule.
One `slow` test runs real Icarus, on a two-cell fixture rather than the PDK
models, to prove an escaped-dot instance's SDF delays land.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
SCRIPTS = ENGINE / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))

import check_glsim  # noqa: E402


def make_ws(tmp_path: Path, with_sdf: bool = True, with_nl: bool = True) -> Path:
    ws = tmp_path / "ws"
    (ws / "spec").mkdir(parents=True)
    (ws / "spec" / "spec.yaml").write_text(
        "top: counter8\nrequirements: []\n"
        "ports: {clk: {dir: input, width: 1}}\n"
        "tt_pins: {clk: clk}\n", encoding="utf-8")
    (ws / "tb").mkdir(parents=True)
    (ws / "tb" / "test_counter8.py").write_text(
        "import cocotb\n@cocotb.test()\nasync def test_x(dut):\n    pass\n",
        encoding="utf-8")
    final = ws / "harden" / "runs" / "run" / "final"
    if with_nl:
        (final / "nl").mkdir(parents=True)
        (final / "nl" / "tt_um_counter8.nl.v").write_text("x", encoding="utf-8")
        (final / "pnl").mkdir(parents=True)
        (final / "pnl" / "tt_um_counter8.pnl.v").write_text("x", encoding="utf-8")
    else:
        final.mkdir(parents=True)
    if with_sdf:
        for _name, corner, _sel in check_glsim.SDF_PASSES:
            (final / "sdf" / corner).mkdir(parents=True)
            (final / "sdf" / corner /
             f"tt_um_counter8__{corner}.sdf").write_text("x", encoding="utf-8")
    return ws


def _patch_pdk(monkeypatch, tmp_path):
    monkeypatch.setattr(check_glsim, "_pdk_root",
                        lambda: Path("/fake/toolchain/foss/pdks"))
    prim = tmp_path / "primitives.v"
    cells = tmp_path / "cells.v"
    prim.write_text("x", encoding="utf-8")
    cells.write_text("x", encoding="utf-8")
    monkeypatch.setattr(check_glsim, "_cell_sources", lambda pdk_root: [prim, cells])


def test_no_harden_output_refuses(tmp_path, capsys):
    ws = tmp_path / "ws"
    (ws / "spec").mkdir(parents=True)
    (ws / "spec" / "spec.yaml").write_text(
        "top: counter8\nrequirements: []\n"
        "ports: {clk: {dir: input, width: 1}}\ntt_pins: {clk: clk}\n",
        encoding="utf-8")
    code = check_glsim.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2, out
    assert "has not run yet" in out["error"]


def test_no_sdf_refuses(tmp_path, capsys):
    ws = make_ws(tmp_path, with_sdf=False)
    code = check_glsim.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2, out
    assert "no SDF" in out["error"]


def test_no_netlist_refuses_inside_run_pass(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path, with_nl=False)
    _patch_pdk(monkeypatch, tmp_path)
    code = check_glsim.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2, out
    assert "no gate-level netlist" in out["error"]


def test_no_results_xml_is_an_error(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    _patch_pdk(monkeypatch, tmp_path)

    class FakeRunner:
        def build(self, **kw):
            pass

        def test(self, **kw):
            pass  # never writes results_xml

    monkeypatch.setattr("cocotb_tools.runner.get_runner", lambda name: FakeRunner())
    code = check_glsim.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 2, out
    assert "produced no" in out["error"]


def test_failing_test_in_either_pass_is_a_violation(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    _patch_pdk(monkeypatch, tmp_path)

    FAIL_XML = """<?xml version="1.0"?>
<testsuites><testsuite><testcase name="test_counter8.test_x" classname="test_counter8">
<failure message="mismatch"/></testcase></testsuite></testsuites>"""
    PASS_XML = """<?xml version="1.0"?>
<testsuites><testsuite><testcase name="test_counter8.test_x" classname="test_counter8">
</testcase></testsuite></testsuites>"""

    class FakeRunner:
        def build(self, **kw):
            pass

        def test(self, results_xml, **kw):
            content = FAIL_XML if "functional" in results_xml else PASS_XML
            Path(results_xml).write_text(content, encoding="utf-8")

    monkeypatch.setattr("cocotb_tools.runner.get_runner", lambda name: FakeRunner())
    code = check_glsim.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 1, out
    assert out["status"] == "violations"
    passes = {v["glsim_pass"] for v in out["violations"]}
    assert passes == {"functional"}
    assert out["results"]["sdf"]["test_x"] is True
    assert out["results"]["functional"]["test_x"] is False


def test_both_passes_all_green_is_a_pass(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    _patch_pdk(monkeypatch, tmp_path)

    PASS_XML = """<?xml version="1.0"?>
<testsuites><testsuite><testcase name="test_counter8.test_x" classname="test_counter8">
</testcase></testsuite></testsuites>"""

    class FakeRunner:
        def build(self, **kw):
            pass

        def test(self, results_xml, **kw):
            Path(results_xml).write_text(PASS_XML, encoding="utf-8")

    monkeypatch.setattr("cocotb_tools.runner.get_runner", lambda name: FakeRunner())
    code = check_glsim.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0, out
    assert out["status"] == "pass"
    assert out["results"]["functional"]["test_x"] is True
    assert out["results"]["sdf"]["test_x"] is True


PASS_XML_X = """<?xml version="1.0"?>
<testsuites><testsuite><testcase name="test_counter8.test_x" classname="test_counter8">
</testcase></testsuite></testsuites>"""


def _runner_writing_logs(build_log_text="", sim_log_text="", calls=None,
                         every_corner=False):
    """A fake cocotb runner that passes every test and writes the given text
    into the nominal sdf pass's build/sim logs (every sdf pass's with
    every_corner), the way Icarus's own messages land there."""
    def gets_text(log_file):
        name = Path(log_file).name
        return ("sdf" in name if every_corner
                else name in ("build_sdf.log", "sim_sdf.log"))

    class FakeRunner:
        def build(self, build_args, log_file, **kw):
            if calls is not None:
                calls.append((log_file, list(build_args)))
            if gets_text(log_file):
                Path(log_file).write_text(build_log_text, encoding="utf-8")

        def test(self, results_xml, log_file, **kw):
            if gets_text(log_file):
                Path(log_file).write_text(sim_log_text, encoding="utf-8")
            Path(results_xml).write_text(PASS_XML_X, encoding="utf-8")
    return FakeRunner()


def _run(ws, monkeypatch, capsys, runner):
    monkeypatch.setattr("cocotb_tools.runner.get_runner", lambda name: runner)
    code = check_glsim.main(["--workspace", str(ws)])
    return code, json.loads(capsys.readouterr().out)


def test_sdf_pass_builds_with_specify_and_interconnect(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    _patch_pdk(monkeypatch, tmp_path)
    calls: list = []
    code, out = _run(ws, monkeypatch, capsys, _runner_writing_logs(calls=calls))
    assert code == 0, out
    args = {Path(log).name: a for log, a in calls}
    assert args["build_functional.log"] == []
    assert args["build_sdf.log"] == ["-gspecify", "-ginterconnect", "-Ttyp"]
    assert args["build_sdf_ff.log"] == ["-gspecify", "-ginterconnect", "-Tmin"]
    assert args["build_sdf_ss.log"] == ["-gspecify", "-ginterconnect", "-Tmax"]
    corners = out["sdf"]["corners"]
    assert {k: (v["corner"], v["delay_selection"]) for k, v in corners.items()} == {
        "sdf": ("nom_tt_025C_3v30", "typ"),
        "sdf_ff": ("min_ff_n40C_3v60", "min"),
        "sdf_ss": ("max_ss_125C_3v00", "max")}
    assert set(out["results"]) == {"functional", "sdf", "sdf_ff", "sdf_ss"}


def test_each_sdf_pass_reads_its_own_corners_sdf(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    _patch_pdk(monkeypatch, tmp_path)
    final = ws / "harden" / "runs" / "run" / "final"
    for _n, corner, _s in check_glsim.SDF_PASSES:
        (final / "sdf" / corner / f"tt_um_counter8__{corner}.sdf").write_text(
            f"(DELAYFILE (DESIGN \"{corner}\"))\n", encoding="utf-8")
    code, out = _run(ws, monkeypatch, capsys, _runner_writing_logs())
    assert code == 0, out
    build = ws / "log" / "glsim_build"
    for name, corner, _s in check_glsim.SDF_PASSES:
        assert corner in (build / f"{name}_icarus.sdf").read_text()
        harness = (build / f"counter8_glsim_{name}.v").read_text()
        assert f"{name}_icarus.sdf" in harness


@pytest.mark.parametrize("corner", ["min_ff_n40C_3v60", "max_ss_125C_3v00"])
def test_a_missing_corner_sdf_refuses(tmp_path, monkeypatch, capsys, corner):
    ws = make_ws(tmp_path)
    _patch_pdk(monkeypatch, tmp_path)
    (ws / "harden" / "runs" / "run" / "final" / "sdf" / corner /
     f"tt_um_counter8__{corner}.sdf").unlink()
    code, out = _run(ws, monkeypatch, capsys, _runner_writing_logs())
    assert code == 2, out
    assert corner in out["error"]


def test_a_failure_at_ff_or_ss_is_reported_in_corner_failures(
        tmp_path, monkeypatch, capsys):
    FAIL_XML = """<?xml version="1.0"?>
<testsuites><testsuite><testcase name="test_counter8.test_x" classname="test_counter8">
<failure message="count 3 != 2"/></testcase></testsuite></testsuites>"""

    class Runner(_runner_writing_logs().__class__):
        def test(self, results_xml, log_file, **kw):
            Path(results_xml).write_text(
                FAIL_XML if "sdf_ss" in results_xml else PASS_XML_X,
                encoding="utf-8")

    ws = make_ws(tmp_path)
    _patch_pdk(monkeypatch, tmp_path)
    code, out = _run(ws, monkeypatch, capsys, Runner())
    # report-only until the benches clock at the spec period
    # (check_glsim.REPORT_ONLY_PASSES); the nominal pass still fails the gate
    assert code == 0, out
    assert out["violations"] == []
    assert [(f["glsim_pass"], f["test"], f["msg"]) for f in out["corner_failures"]] == [
        ("sdf_ss", "test_x", "count 3 != 2")]
    assert out["results"]["sdf"]["test_x"] is True
    assert out["results"]["sdf_ss"]["test_x"] is False


def test_unsupported_timing_checks_are_reported_not_implied(
        tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    _patch_pdk(monkeypatch, tmp_path)
    code, out = _run(ws, monkeypatch, capsys, _runner_writing_logs(
        build_log_text="cells.v:9: warning: Timing checks are not supported.\n",
        every_corner=True))
    assert code == 0, out
    for c in out["sdf"]["corners"].values():
        assert c["timing_checks"].startswith("not simulated (Icarus"), c


def test_timing_checks_say_not_simulated_even_without_the_warning(
        tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    _patch_pdk(monkeypatch, tmp_path)
    code, out = _run(ws, monkeypatch, capsys, _runner_writing_logs())
    assert code == 0, out
    for c in out["sdf"]["corners"].values():
        assert c["timing_checks"] == "not simulated", c


def test_sdf_annotate_omitted_at_the_slow_corner_refuses(
        tmp_path, monkeypatch, capsys):
    class Runner(_runner_writing_logs().__class__):
        def test(self, results_xml, log_file, **kw):
            if Path(log_file).name == "sim_sdf_ss.log":
                Path(log_file).write_text(
                    "h.v:30: warning: Omitting $sdf_annotate() since specify "
                    "blocks and interconnects are being omitted.\n",
                    encoding="utf-8")
            Path(results_xml).write_text(PASS_XML_X, encoding="utf-8")

    ws = make_ws(tmp_path)
    _patch_pdk(monkeypatch, tmp_path)
    code, out = _run(ws, monkeypatch, capsys, Runner())
    assert code == 2, out
    assert "sdf_ss pass" in out["error"]


def test_sdf_annotate_omitted_refuses(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    _patch_pdk(monkeypatch, tmp_path)
    code, out = _run(ws, monkeypatch, capsys, _runner_writing_logs(
        sim_log_text="counter8_glsim_sdf.v:30: warning: Omitting $sdf_annotate() "
                     "since specify blocks and interconnects are being omitted.\n"))
    assert code == 2, out
    assert "Omitting $sdf_annotate" in out["error"]


def test_sdf_error_refuses(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    _patch_pdk(monkeypatch, tmp_path)
    code, out = _run(ws, monkeypatch, capsys, _runner_writing_logs(
        sim_log_text="SDF ERROR: x.sdf:15: Could not find intermodpath!\n"))
    assert code == 2, out
    assert "SDF ERROR" in out["error"]


def _waiver_ws(tmp_path, monkeypatch):
    """A workspace whose SDF has an a_xor cell (Icarus refused its paths at
    build time) at _50_, IOPATH on line 7 of the copy handed to Icarus, and
    an a_buf cell at _51_, IOPATH on line 14."""
    ws = make_ws(tmp_path)
    _patch_pdk(monkeypatch, tmp_path)
    cells = tmp_path / "cells.v"
    cells.write_text("module a_buf(A, Z);\nendmodule\n"
                     "module a_xor(A1, A2, Z);\n specify\n"
                     " ifnone (posedge A1 => (Z:A1)) = (1.0,1.0);\n"
                     " endspecify\nendmodule\n", encoding="utf-8")
    sdf = (ws / "harden" / "runs" / "run" / "final" / "sdf" / check_glsim.SDF_CORNER /
           f"tt_um_counter8__{check_glsim.SDF_CORNER}.sdf")
    sdf.write_text(
        '(DELAYFILE\n'
        ' (CELL\n  (CELLTYPE "a_xor")\n  (INSTANCE _50_)\n  (DELAY\n   (ABSOLUTE\n'
        '    (IOPATH A1 Z (0.1:0.1:0.1) (0.2:0.2:0.2))\n   )\n  )\n )\n'
        ' (CELL\n  (CELLTYPE "a_buf")\n  (INSTANCE _51_)\n  (DELAY\n   (ABSOLUTE\n'
        '    (IOPATH A Z (0.1:0.1:0.1) (0.2:0.2:0.2))\n   )\n  )\n )\n'
        ' (CELL (CELLTYPE "tt_um_counter8") (INSTANCE) (DELAY (ABSOLUTE\n'
        '    (INTERCONNECT tie.ZN uio_oe[0] (0.000:0.000:0.000))\n'
        '    (INTERCONNECT a.Z b.A (0.118:0.118:0.118) (0.063:0.063:0.063))\n'
        ')))\n)\n', encoding="utf-8")
    build = f"{cells}:5: sorry: ifnone with an edge-sensitive path is not supported.\n"
    icarus = ws / "log" / "glsim_build" / "sdf_icarus.sdf"
    return ws, build, icarus


def test_unmatched_modpath_on_a_cell_icarus_refused_is_waived(tmp_path, monkeypatch, capsys):
    ws, build, icarus = _waiver_ws(tmp_path, monkeypatch)
    ok_sim = f"SDF ERROR: {icarus}:7: Unable to match ModPath A1 -> Z in counter8.dut._50_\n"
    code, out = _run(ws, monkeypatch, capsys, _runner_writing_logs(build, ok_sim))
    assert code == 0, out
    assert out["sdf"]["sdf_unannotated"] == ["_50_ (a_xor)"]
    assert out["sdf"]["zero_interconnects_dropped"] == 1
    icarus_sdf = icarus.read_text()
    assert "tie.ZN" not in icarus_sdf and "a.Z b.A" in icarus_sdf

    # the same mismatch on a cell Icarus did NOT refuse is a real SDF error
    bad_sim = f"SDF ERROR: {icarus}:14: Unable to match ModPath A -> Z in counter8.dut._51_\n"
    code, out = _run(ws, monkeypatch, capsys, _runner_writing_logs(build, bad_sim))
    assert code == 2, out
    assert "_51_" in out["error"]


def test_unmatched_modpath_torn_by_cocotb_output_is_waived_by_line(tmp_path, monkeypatch, capsys):
    # cocotb's stdout shares the sim log and can cut Icarus's line before the
    # instance name; the SDF line number still says which cell it was
    ws, build, icarus = _waiver_ws(tmp_path, monkeypatch)
    torn = (f"SDF ERROR: {icarus}:7: Unable to match ModP  1345.00ns INFO     "
            "cocotb.regression  test_counter8.test_x passed\n"
            "ath A1 -> Z in counter8.dut._50_\n")
    code, out = _run(ws, monkeypatch, capsys, _runner_writing_logs(build, torn))
    assert code == 0, out
    assert out["sdf"]["sdf_unannotated"] == ["_50_ (a_xor)"]


def test_sdf_error_the_gate_cannot_attribute_still_refuses(tmp_path, monkeypatch, capsys):
    ws, build, icarus = _waiver_ws(tmp_path, monkeypatch)
    for sim in (
            # torn line pointing at a cell Icarus did not refuse
            f"SDF ERROR: {icarus}:14: Unable to match Mo  12.00ns INFO x\n",
            # torn before the line number
            f"SDF ERROR: {str(icarus)[:12]}  12.00ns INFO x\n",
            # torn too early to tell ModPath from "Unable to find ..."
            f"SDF ERROR: {icarus}:7: Unable to  12.00ns INFO x\n",
            # another kind of SDF error on the refused cell's line
            f"SDF ERROR: {icarus}:7: Could not find intermodpath!\n",
            # a line of the refused cell that is not an IOPATH
            f"SDF ERROR: {icarus}:4: Unable to match ModPath A1 -> Z in counter8.dut._50_\n",
            # intact text naming a different instance than the line holds
            f"SDF ERROR: {icarus}:7: Unable to match ModPath A1 -> Z in counter8.dut._51_\n",
            # a line number past the end of the file
            f"SDF ERROR: {icarus}:999: Unable to match ModPath A1 -> Z in counter8.dut._50_\n",
            # some other SDF file
            f"SDF ERROR: {tmp_path}/other.sdf:7: Unable to match ModPath A1 -> Z in counter8.dut._50_\n",
    ):
        code, out = _run(ws, monkeypatch, capsys, _runner_writing_logs(build, sim))
        assert code == 2, (sim, out)
        assert "not fully applied" in out["error"], (sim, out)


ESCAPED = REPO / "tests" / "fixtures" / "glsim_escaped_dot"


def _escaped_ws(tmp_path, monkeypatch, nl_text=None):
    """A workspace whose netlist and SDF are the escaped-dot fixture: CTS
    named the buffer on net `u.clk` `\\clkbuf_u.clk `, and the SDF reaches
    it from an INTERCONNECT and its own CELL block."""
    ws = make_ws(tmp_path)
    _patch_pdk(monkeypatch, tmp_path)
    final = ws / "harden" / "runs" / "run" / "final"
    for view, name in (("nl", "nl"), ("pnl", "pnl")):
        (final / view / f"tt_um_counter8.{name}.v").write_text(
            nl_text if nl_text is not None else (ESCAPED / "top.nl.v").read_text(),
            encoding="utf-8")
    (final / "sdf" / check_glsim.SDF_CORNER /
     f"tt_um_counter8__{check_glsim.SDF_CORNER}.sdf").write_text(
        (ESCAPED / "top.sdf").read_text(), encoding="utf-8")
    return ws


def test_escaped_dot_instance_is_renamed_in_the_sdf_passs_own_copies(
        tmp_path, monkeypatch, capsys):
    ws = _escaped_ws(tmp_path, monkeypatch)
    built = {}

    class Runner:
        def build(self, sources, log_file, **kw):
            built[Path(log_file).name] = sources

        def test(self, results_xml, **kw):
            Path(results_xml).write_text(PASS_XML_X, encoding="utf-8")

    code, out = _run(ws, monkeypatch, capsys, Runner())
    assert code == 0, out
    # `clkbuf_u__clk` is already a net there, so the rename steps past it
    assert out["sdf"]["instances_renamed"] == {"clkbuf_u.clk": "clkbuf_u__clk_1"}
    assert out["sdf"]["sdf_unannotated"] == []
    build_dir = ws / "log" / "glsim_build"
    sdf = (build_dir / "sdf_icarus.sdf").read_text()
    assert "\\." not in sdf
    assert "(INTERCONNECT _1_.Z clkbuf_u__clk_1.A " in sdf
    assert "(INSTANCE clkbuf_u__clk_1)" in sdf
    nl = (build_dir / "sdf_icarus.nl.v").read_text()
    assert "a_buf clkbuf_u__clk_1  (.A(\\u.clk ), .Z(z));" in nl
    assert "wire \\u.clk ;" in nl and "wire clkbuf_u__clk;" in nl
    # only the sdf pass builds from the renamed copy
    assert str(build_dir / "sdf_icarus.nl.v") in built["build_sdf.log"]
    nl_src = ws / "harden" / "runs" / "run" / "final" / "pnl" / "tt_um_counter8.pnl.v"
    assert str(nl_src) in built["build_functional.log"]
    assert str(nl_src) not in built["build_sdf.log"]


def test_sdf_naming_an_escaped_instance_the_netlist_lacks_refuses(
        tmp_path, monkeypatch, capsys):
    nl = (ESCAPED / "top.nl.v").read_text().replace("\\clkbuf_u.clk ", "cb ")
    ws = _escaped_ws(tmp_path, monkeypatch, nl_text=nl)
    code, out = _run(ws, monkeypatch, capsys, _runner_writing_logs())
    assert code == 2, out
    assert "clkbuf_u\\.clk" in out["error"]


@pytest.mark.slow
def test_escaped_dot_interconnect_annotates_under_real_icarus(tmp_path):
    """Icarus aborts vvp on an INTERCONNECT to `clkbuf_u\\.clk.A` ("NULL
    handle passed to vpi_scan"); from the renamed copies it runs, and both
    the 1 ns INTERCONNECT and the 2 ns IOPATH land on the buffer."""
    nl_text = (ESCAPED / "top.nl.v").read_text()
    renames = check_glsim.escaped_dot_renames(
        (ESCAPED / "top.sdf").read_text(), nl_text)
    sdf, nl = tmp_path / "top.sdf", tmp_path / "top.nl.v"
    check_glsim.sdf_for_icarus(ESCAPED / "top.sdf", sdf, renames)
    check_glsim.netlist_for_icarus(ESCAPED / "top.nl.v", nl, renames)
    vvp = tmp_path / "sim.vvp"
    eda = str(check_glsim.EDA_BIN)
    build = subprocess.run(
        [eda, "iverilog", "-gspecify", "-ginterconnect", f'-DSDF="{sdf}"',
         "-o", str(vvp), str(ESCAPED / "tb.v"), str(nl), str(ESCAPED / "cells.v")],
        capture_output=True, text=True)
    assert build.returncode == 0, build.stderr
    run = subprocess.run([eda, "vvp", "-n", str(vvp)],
                         capture_output=True, text=True, timeout=60)
    log = run.stdout + run.stderr
    assert run.returncode == 0, log
    assert "SDF ERROR" not in log, log
    assert "z rose at 8000" in log, log


# ------------------------------------------------- the netlist the tile ships (#149)

NL = """module t(input a, output y);
 wire n;
 inv_1 u1 (.I(a),
    .ZN(n));
 inv_1 \\u.2  (.I(n),
    .ZN(y));
 filltie_1 tap ();
endmodule
"""
PNL = """module t(input a, output y, inout VPWR, inout VGND);
 wire n;
 inv_1 u1 (.I(a),
    .ZN(n),
    .VDD(VPWR),
    .VNW(VPWR),
    .VPW(VGND),
    .VSS(VGND));
 inv_1 \\u.2  (.I(n),
    .ZN(y),
    .VDD(VPWR),
    .VNW(VPWR),
    .VPW(VGND),
    .VSS(VGND));
 filltie_1 tap (.VDD(VPWR),
    .VNW(VPWR),
    .VPW(VGND),
    .VSS(VGND));
endmodule
"""


def _netlists(ws, nl=NL, pnl=PNL):
    final = ws / "harden" / "runs" / "run" / "final"
    (final / "nl" / "tt_um_counter8.nl.v").write_text(nl)
    (final / "pnl" / "tt_um_counter8.pnl.v").write_text(pnl)
    return final


def test_functional_pass_simulates_the_powered_netlist(tmp_path, monkeypatch, capsys):
    ws = make_ws(tmp_path)
    _patch_pdk(monkeypatch, tmp_path)
    final = _netlists(ws)
    built = {}

    class Runner(_runner_writing_logs().__class__):
        def build(self, sources, log_file, **kw):
            built[Path(log_file).name] = sources

    code, out = _run(ws, monkeypatch, capsys, Runner())
    assert code == 0, out
    assert str(final / "pnl/tt_um_counter8.pnl.v") in built["build_functional.log"]
    assert not any("sdf_icarus" in s or ".nl.v" in s for s in built["build_functional.log"])
    assert any(s.endswith("sdf_icarus.nl.v") for s in built["build_sdf.log"])
    defines = Path(built["build_functional.log"][0]).read_text()
    assert "`define FUNCTIONAL" in defines and "`define USE_POWER_PINS" in defines
    # what release reads to tie the shipped netlist to this run
    assert out["netlists"]["pnl_sha256"] == check_glsim.ttship._sha256(
        final / "pnl/tt_um_counter8.pnl.v")
    assert out["netlists"]["nl_sha256"] == check_glsim.ttship._sha256(
        final / "nl/tt_um_counter8.nl.v")


def test_powered_netlist_of_another_design_is_a_violation(tmp_path, monkeypatch, capsys):
    # sdf and lec read final/nl, the tile ships final/pnl: one swapped cell
    # between them, and neither gate looks at both
    ws = make_ws(tmp_path)
    _patch_pdk(monkeypatch, tmp_path)
    _netlists(ws, pnl=PNL.replace("inv_1 u1", "buf_1 u1"))
    code, out = _run(ws, monkeypatch, capsys, _runner_writing_logs())
    assert code == 1, out
    assert [v["kind"] for v in out["violations"]] == ["pnl_differs_from_nl"]
    assert "u1" in out["violations"][0]["msg"]


@pytest.mark.parametrize("nl,pnl", [
    (NL, PNL.replace(".I(n)", ".I(a)")),
    (NL, PNL.replace(" filltie_1 tap", " filltie_2 tap")),
    (NL.replace(" filltie_1 tap ();\n", ""), PNL),
])
def test_compare_netlists_sees_a_connection_or_a_cell(tmp_path, nl, pnl):
    a, b = tmp_path / "a.v", tmp_path / "b.v"
    a.write_text(nl)
    b.write_text(pnl)
    assert check_glsim.compare_netlists(a, b)


def test_compare_netlists_ignores_only_the_power_pins(tmp_path):
    a, b = tmp_path / "a.v", tmp_path / "b.v"
    a.write_text(NL)
    b.write_text(PNL)
    assert check_glsim.compare_netlists(a, b) is None
    assert [c[0] for c in check_glsim.netlist_cells(NL)] == ["filltie_1", "inv_1", "inv_1"]


def test_port_alias_collapses_onto_the_port_and_nothing_else_moves():
    text = (
        "module t (a, b, c, d);\n"
        " output [1:0] a;\n output b;\n output c;\n input d;\n"
        " wire net115;\n wire net1150;\n wire net7;\n"
        " buf_1 place115 (.I(d), .Z(net115));\n"
        " buf_1 load (.I(net115), .Z(net1150));\n"
        " assign a[1] = net115;\n"
        " assign b = net115;\n"
        " assign c = d;\n"
        " assign a[0] = 1'b0;\n"
        "endmodule\n")
    out = check_glsim.collapse_port_aliases(text)
    assert "wire net115;" not in out and "assign a[1]" not in out
    assert ".Z(a[1])" in out and ".I(a[1])" in out
    assert "wire net1150;" in out and ".Z(net1150)" in out
    # a second port on the net keeps its assign, now from the first port
    assert "assign b = a[1];" in out
    # a port on the right, not a declared wire, and a constant stay as written
    assert "assign c = d;" in out and "assign a[0] = 1'b0;" in out


def test_netlist_for_icarus_collapses_port_aliases(tmp_path):
    src, dest = tmp_path / "in.v", tmp_path / "out.v"
    src.write_text(" wire n1;\n buf_1 u (.I(x), .Z(n1));\n"
                   " assign p[3] = n1;\n", encoding="utf-8")
    check_glsim.netlist_for_icarus(src, dest, {})
    assert dest.read_text() == " buf_1 u (.I(x), .Z(p[3]));\n"
