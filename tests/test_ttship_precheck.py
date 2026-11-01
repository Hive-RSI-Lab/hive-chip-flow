"""The workflow ttship stages, on the tree it stages, passes what TT runs
(#149). Real tools and a recorded run, so `slow`; skipped where that run isn't
on disk (it is ece298a-pll-digital's, read-only).

custom_gds is replayed from the staged workflow's own inputs, so a path the
workflow gets wrong fails here, then the vendored precheck and the staged
Makefile run on the result. The Actions jobs themselves don't run."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import check_precheck  # noqa: E402
import ttlib  # noqa: E402
import ttship  # noqa: E402

PLL = Path(os.environ.get("PLL_DIGITAL_WS", Path.home() / "projects/ece298a-pll-digital/blocks/pll/digital"))
pytestmark = [pytest.mark.slow,
              pytest.mark.skipif(not (PLL / "harden/runs/run/final/pnl").is_dir(),
                                 reason="no recorded PLL digital run")]


@pytest.fixture(scope="module")
def tree(tmp_path_factory):
    dest = tmp_path_factory.mktemp("tt") / "tt"
    sign = ttship.signoff(PLL)
    ttship.stage(PLL, dest, sign)
    return dest, sign


def custom_gds_inputs(tree: Path) -> dict:
    doc = yaml.safe_load((tree / ".github/workflows/gds.yaml").read_text())
    return next(s["with"] for s in doc["jobs"]["gds"]["steps"]
                if "custom_gds" in str(s.get("uses")))


def replay_custom_gds(tree: Path, top: str, dest: Path, empty_netlist=False) -> Path:
    """tt_submission/ as custom_gds@ttgf26c "Copy GDS / LEF files" writes it,
    laid out as precheck.py wants it, around a copy of the vendored tool."""
    inputs = custom_gds_inputs(tree)
    shutil.copytree(ttlib.PRECHECK_DIR, dest / "precheck")
    (dest / "precheck" / "reports").mkdir(exist_ok=True)
    shutil.copytree(ttlib.SUPPORT_DIR / "tech", dest / "tech")
    sub = dest / "design"
    sub.mkdir()
    for kind, ext in (("gds_path", "gds"), ("lef_path", "lef"), ("verilog_path", "v")):
        rel = ttship._resolve_path(inputs[kind], top)
        shutil.copyfile(tree / rel, sub / f"{top}.{ext}")
    if empty_netlist:
        (sub / f"{top}.v").write_text("")
    shutil.copyfile(tree / "info.yaml", dest / "info.yaml")
    return dest


def precheck_failures(run_dir: Path, top: str) -> tuple[int, dict]:
    _, results_xml, output = check_precheck.invoke_precheck(run_dir, top)
    assert results_xml.is_file(), output[-2000:]
    cases = list(ET.parse(results_xml).iter("testcase"))
    return len(cases), {c.attrib["name"]: c.find("error").attrib.get("message")
                        for c in cases if c.find("error") is not None}


def test_staged_workflow_and_tree_pass_vendored_precheck(tree, tmp_path):
    repo, sign = tree
    top = sign["top_module"]
    problems, _ = ttship.ship_problems(repo, {**sign, "glsim_gl_sha256": sign["gl_sha256"]})
    assert problems == []
    n, failed = precheck_failures(replay_custom_gds(repo, top, tmp_path), top)
    assert failed == {} and n >= 10, failed


def test_without_the_netlist_precheck_fails_on_power_pins(tree, tmp_path):
    # the control: what the workflow staged before #149 shipped
    repo, sign = tree
    top = sign["top_module"]
    _, failed = precheck_failures(
        replay_custom_gds(repo, top, tmp_path, empty_netlist=True), top)
    assert "VGND" in str(failed), failed


def run_staged_makefile(repo: Path, tmp_path: Path, netlist: bytes) -> ET.ElementTree:
    """The gl_test job's run step on a copy of test/, cocotb-config on PATH."""
    test = tmp_path / "test"
    shutil.copytree(repo / "test", test)
    (test / "gate_level_netlist.v").write_bytes(netlist)
    shim = tmp_path / "bin"
    shim.mkdir()
    cfg = shim / "cocotb-config"
    cfg.write_text(f'#!/bin/sh\nexec "{REPO}/bin/eda" cocotb-config "$@"\n')
    cfg.chmod(0o755)
    env = {**os.environ, "GATES": "yes", "PATH": f"{shim}:{os.environ['PATH']}",
           "PDK_ROOT": str(check_precheck._pdk_root())}
    proc = subprocess.run(["make"], cwd=test, env=env, capture_output=True, text=True,
                          timeout=600)
    assert (test / "results.xml").is_file(), (proc.stdout + proc.stderr)[-3000:]
    return ET.parse(test / "results.xml")


def test_staged_makefile_passes_on_the_shipped_netlist(tree, tmp_path):
    repo, sign = tree
    results = run_staged_makefile(repo, tmp_path, (repo / "gl" / f"{sign['top_module']}.v").read_bytes())
    cases = list(results.iter("testcase"))
    assert cases and not [c for c in cases if c.find("failure") is not None
                          or c.find("error") is not None]


def test_staged_makefile_fails_on_a_broken_netlist(tree, tmp_path):
    # swap every output inverter/buffer on uo_out for a stuck tie-off's cell:
    # a job that always passes would pass this
    repo, sign = tree
    text = (repo / "gl" / f"{sign['top_module']}.v").read_text()
    broken = text.replace(".Z(uo_out[0])", ".Z(uo_out_unused0)").replace(
        ".Z(uo_out[1])", ".Z(uo_out_unused1)")
    assert broken != text
    results = run_staged_makefile(repo, tmp_path, broken.encode())
    assert [c for c in results.iter("testcase") if c.find("failure") is not None]


def test_glsim_netlist_read_from_the_recorded_gate_result(tmp_path):
    """gate.py records glsim under reports/recorded with the check's fields in
    facts; that is the only copy a gate run leaves."""
    rec = tmp_path / "reports" / "recorded"
    rec.mkdir(parents=True)
    (rec / "gate-glsim.json").write_text(json.dumps(
        {"facts": {"netlists": {"pnl_sha256": "abc"}}}), encoding="utf-8")
    assert ttship._glsim_netlist(tmp_path) == "abc"
    assert ttship._glsim_netlist(tmp_path / "none") is None
