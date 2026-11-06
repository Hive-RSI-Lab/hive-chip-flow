"""check_harden.py's connectivity check over the final ODB (#156). The PLL's
digital harden is the known answer, as net_connectivity.tcl's recorded output:
the ODB stays with its project. The faults planted in a real counter8 ODB are in
tests/test_corpus_faults.py, in the lane that has the harden to plant them in."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import check_harden  # noqa: E402
import ttlib  # noqa: E402
from test_check_harden import (CLEAN_CONNECTIVITY, _fake_run_factory,  # noqa: E402
                               make_ws)

RECORDED = (REPO / "tests" / "fixtures" / "harden_signoff" / "pll_digital"
            / "connectivity.txt")
PLL_INPUTS = ["ena", "ui_in[4]", "ui_in[5]", "uio_in[2]", "uio_in[3]",
              "uio_in[4]", "uio_in[5]", "uio_in[6]", "uio_in[7]"]
TOP = "tt_um_x"


def row(net, drivers=1, loads=1, inputs=()):
    return {"net": net, "drivers": drivers, "loads": loads,
            "inputs": list(inputs)}


def judge(*rows, waived=None):
    signoff = {"floating_nets_waived": waived} if waived else {}
    return check_harden.judge_connectivity(100, list(rows), signoff, TOP)


def named(violations):
    return {v["kind"]: v["refs"] for v in violations}


def test_the_pll_known_answer_is_no_undriven_net_and_its_nine_inputs():
    seen, rows = check_harden.parse_connectivity(RECORDED.read_text("utf-8"))
    table, violations = check_harden.judge_connectivity(seen, rows, {}, TOP)
    assert violations == []
    assert table["undriven"] == table["unloaded"] == []
    assert table["tt_inputs_unloaded"] == PLL_INPUTS


@pytest.mark.slow
@pytest.mark.skipif("CHIP_FLOW_PLL_FINAL_ODB" not in os.environ,
                    reason="CHIP_FLOW_PLL_FINAL_ODB names no PLL final ODB")
def test_the_recorded_known_answer_is_what_the_pll_odb_says(tmp_path):
    odb = Path(os.environ["CHIP_FLOW_PLL_FINAL_ODB"])
    assert check_harden.read_connectivity(odb, tmp_path) == \
        check_harden.parse_connectivity(RECORDED.read_text("utf-8"))


def test_a_net_with_no_driver_is_named_and_refused():
    table, violations = judge(row("n1", drivers=0))
    assert named(violations) == {"net_undriven": ["n1"]}
    assert "n1" in violations[0]["msg"]
    assert table["undriven"] == ["n1"]


def test_a_waiver_never_excuses_a_net_with_no_driver():
    table, violations = judge(row("n1", drivers=0), row("n2", drivers=0, loads=0),
                              waived={"n1": "why", "n2": "why"})
    assert named(violations) == {"net_undriven": ["n1", "n2"]}
    assert table["waived"] == []
    assert table["unused_waivers"] == ["n1", "n2"]


def test_a_net_with_no_load_is_named_and_refused():
    _, violations = judge(row("n1", loads=0), row("n2", loads=0))
    assert named(violations) == {"net_unloaded": ["n1", "n2"]}


def test_a_waiver_must_name_the_net_with_no_load():
    table, violations = judge(row("n1", loads=0), row("n2", loads=0),
                              waived={"n2": "why", "gone": "why"})
    assert named(violations) == {"net_unloaded": ["n1"]}
    assert table["waived"] == ["n2"]
    assert table["unused_waivers"] == ["gone"]


@pytest.mark.parametrize("port", ["clk", "rst_n", "ena", "ui_in[0]", "uio_in[7]"])
def test_a_wrapper_input_may_have_no_load(port):
    table, violations = judge(row(port, loads=0, inputs=[port]))
    assert violations == []
    assert table["tt_inputs_unloaded"] == [port]


@pytest.mark.parametrize("port", ["other", "uo_out[0]", "uio_out[1]", "uio_oe[2]",
                                  "ua[0]"])
def test_no_other_port_is_a_wrapper_input(port):
    assert not ttlib.is_wrapper_input(port)
    _, violations = judge(row(port, loads=0, inputs=[port]))
    assert named(violations) == {"net_unloaded": [port]}


def test_an_escaped_name_is_reported_as_the_spec_writes_it():
    seen, rows = check_harden.parse_connectivity(
        "CONN_NET\tu\\[1\\].n\t1\t0\tui_in\\[3\\],other\nCONN_END\t5\n")
    assert seen == 5
    assert rows == [row("u[1].n", loads=0, inputs=["ui_in[3]", "other"])]


def test_a_list_with_no_end_row_says_nothing():
    assert check_harden.parse_connectivity("CONN_NET\tn\t0\t1\t\n") is None


def flow_with_odb(cwd):
    (cwd / "runs" / "run").mkdir(parents=True, exist_ok=True)
    (cwd / "runs" / "run" / "flow.log").write_text("Flow complete.\n", "utf-8")
    final = cwd / "runs" / "run" / "final"
    for fmt in check_harden.EXPECTED_FORMATS[:-1] + ("odb",):
        (final / fmt).mkdir(parents=True, exist_ok=True)
        (final / fmt / f"tt_um_counter8.{fmt}").write_text("x", "utf-8")
    (final / "metrics.json").write_text(json.dumps({}), "utf-8")
    return subprocess.CompletedProcess(["eda", "librelane"], 0, stdout="", stderr="")


def harden(tmp_path, monkeypatch, capsys, behavior, connectivity):
    ws = make_ws(tmp_path)
    monkeypatch.setattr(check_harden.subprocess, "run",
                        _fake_run_factory(behavior, connectivity))
    code = check_harden.main(["--workspace", str(ws)])
    return code, json.loads(capsys.readouterr().out)


def test_the_gate_reports_the_nets_of_the_final_odb(tmp_path, monkeypatch, capsys):
    connectivity = subprocess.CompletedProcess(
        ["eda"], 0, stderr="", stdout="CONN_NET\tcut\t0\t1\t\n"
        "CONN_NET\tena\t1\t0\tena\nCONN_END\t80\n")
    code, out = harden(tmp_path, monkeypatch, capsys, flow_with_odb, connectivity)
    assert code == 1, out
    found = [v for v in out["violations"] if v["kind"] == "net_undriven"]
    assert [v["refs"] for v in found] == [["cut"]]
    assert out["connectivity"]["nets"] == 80
    assert out["connectivity"]["tt_inputs_unloaded"] == ["ena"]


def test_a_flow_with_no_final_odb_never_passes(tmp_path, monkeypatch, capsys):
    def no_odb(cwd):
        done = flow_with_odb(cwd)
        (cwd / "runs" / "run" / "final" / "odb" / "tt_um_counter8.odb").unlink()
        return done

    code, out = harden(tmp_path, monkeypatch, capsys, no_odb, CLEAN_CONNECTIVITY)
    assert code == 1, out
    assert any(v["kind"] == "harden_missing_artifact" and ".odb" in v["msg"]
               for v in out["violations"])
    assert out["connectivity"] is None


@pytest.mark.parametrize("returncode, stdout", [
    (1, "CONN_END\t80\n"),         # openroad failed after the end row
    (0, "CONN_NET\tn\t0\t1\t\n"),  # cut short
    (0, "CONN_END\t0\n"),          # read a design with no nets
    (0, ""),
])
def test_a_read_that_says_nothing_is_a_refusal(tmp_path, monkeypatch, capsys,
                                               returncode, stdout):
    failed = subprocess.CompletedProcess(["eda"], returncode, stdout=stdout, stderr="")
    code, out = harden(tmp_path, monkeypatch, capsys, flow_with_odb, failed)
    assert code == 2, out
    assert out["status"] == "error"
    assert "connectivity" in json.dumps(out)
