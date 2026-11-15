"""SystemVerilog through the vde gates (#91): tests/fixtures/sv_fsm is a
package (typedef enum, packed struct) imported by a module with always_ff /
always_comb, and it must pass lint, sim, synth and formal. synth reads it
through yosys-slang (check_synth.pick_frontend); an all-.v design keeps
read_verilog, so the Verilog corpus synthesizes exactly as before. bin/eda
runs sv2v, slang-tidy and slang-hier. The slow tests need the eda image."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import check_formal  # noqa: E402
import check_lint  # noqa: E402
import check_sim  # noqa: E402
import check_synth  # noqa: E402
import checklib  # noqa: E402
from checklib import CheckError  # noqa: E402

FIXTURE = REPO / "tests" / "fixtures" / "sv_fsm"
EDA = REPO / "bin" / "eda"


def _ws(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    shutil.copytree(FIXTURE, ws)
    (ws / "log").mkdir(exist_ok=True)
    return ws


# ---- fast: file order and frontend choice ---------------------------------

def test_package_files_are_read_first(tmp_path):
    rtl = tmp_path / "rtl"
    rtl.mkdir()
    a = rtl / "a_top.sv"
    a.write_text("module a_top import z_pkg::*; (); endmodule\n")
    z = rtl / "z_pkg.sv"
    z.write_text("// types\npackage z_pkg;\n  typedef logic [1:0] t;\nendpackage\n")
    v = rtl / "b.v"
    v.write_text("module b; endmodule\n")
    assert checklib.order_sources([v, a, z]) == [z, v, a]


def test_verilog_only_order_is_unchanged(tmp_path):
    files = []
    for n in ("c.v", "a.v", "b.v"):
        f = tmp_path / n
        # `package` in a .v file is not SystemVerilog's: order stays put
        f.write_text("package x;\nendpackage\n" if n == "b.v" else "")
        files.append(f)
    assert checklib.order_sources(files) == files


def test_verilog_design_keeps_read_verilog(tmp_path):
    def no_root():
        raise AssertionError("a .v design must not even look for slang")
    files = [tmp_path / "rtl" / "top.v"]
    assert check_synth.pick_frontend(files, no_root) == ("verilog", None)
    lines = check_synth.read_lines(tmp_path, "top", files, None)
    assert lines == "read_verilog rtl/top.v"


def test_sv_design_reads_through_slang(tmp_path):
    so = tmp_path / check_synth.SLANG_SO
    so.parent.mkdir(parents=True)
    so.write_text("")
    files = [tmp_path / "rtl" / "p.sv", tmp_path / "rtl" / "top.sv"]
    fe, got = check_synth.pick_frontend(files, lambda: tmp_path)
    assert (fe, got) == ("slang", so)
    lines = check_synth.read_lines(tmp_path, "top", files, so).splitlines()
    assert lines == [f"plugin -i {so}", "read_slang --top top rtl/p.sv rtl/top.sv"]


def test_sv_design_without_the_plugin_is_refused(tmp_path):
    with pytest.raises(CheckError, match="no yosys-slang plugin"):
        check_synth.pick_frontend([tmp_path / "top.sv"], lambda: tmp_path)


# ---- slow: the fixture through every gate -------------------------------------

def _run(mod, ws, capsys):
    code = mod.main(["--workspace", str(ws)])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["status"] == "pass", out
    return out


@pytest.mark.slow
def test_sv_fixture_passes_lint_sim_synth_formal(tmp_path, capsys):
    ws = _ws(tmp_path)
    lint = _run(check_lint, ws, capsys)
    assert lint["files"][0] == "rtl/sv_fsm_pkg.sv"
    sim = _run(check_sim, ws, capsys)
    assert sim["tests_run"] == ["test_start_runs_ten_then_done"]
    synth = _run(check_synth, ws, capsys)
    assert synth["frontend"] == "slang"
    assert synth["cell_count"] > 10
    formal = _run(check_formal, ws, capsys)
    assert formal["proven"] == ["REQ-FORMAL-EXCL"]


@pytest.mark.slow
def test_sv2v_lowering_synths_alike(tmp_path, capsys):
    """sv2v's plain-Verilog lowering, read by read_verilog, lands within a
    few cells of the slang read: the same logic both ways (abc's mapping
    is not bit-identical across two different netlists, so not equal)."""
    ws = _ws(tmp_path)
    slang = _run(check_synth, ws, capsys)
    v = tmp_path / "v"
    shutil.copytree(FIXTURE, v)
    (v / "log").mkdir(exist_ok=True)
    srcs = [str(f) for f in checklib.order_sources(sorted((v / "rtl").glob("*.sv")))]
    lowered = subprocess.run([str(EDA), "sv2v", *srcs], capture_output=True,
                             text=True, timeout=120)
    assert lowered.returncode == 0, lowered.stderr
    for f in (v / "rtl").glob("*.sv"):
        f.unlink()
    (v / "rtl" / "sv_fsm.v").write_text(lowered.stdout)
    plain = _run(check_synth, v, capsys)
    assert plain["frontend"] == "verilog"
    a, b = slang["cell_count"], plain["cell_count"]
    assert abs(a - b) <= max(2, a // 10), (a, b)
    assert slang["cells"].keys() & plain["cells"].keys()


@pytest.mark.slow
@pytest.mark.parametrize("tool", ["sv2v", "slang-tidy", "slang-hier"])
def test_eda_runs_the_sv_tools(tool):
    r = subprocess.run([str(EDA), tool, "--version"], capture_output=True,
                       text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip()


@pytest.mark.slow
def test_slang_tidy_passes_the_fixture():
    srcs = [str(f) for f in sorted((FIXTURE / "rtl").glob("*.sv"))]
    r = subprocess.run([str(EDA), "slang-tidy", *srcs], capture_output=True,
                       text=True, timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "FAIL" not in r.stdout
