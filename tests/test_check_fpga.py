"""engine/scripts/check_fpga.py (#96): the optional FPGA prototype step.
Pure helpers run unmarked; the real yosys + nextpnr-ice40 runs on corpus
counter8 are `slow` (corpus counter8's fault rung also runs them in CI)."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "scripts"))
sys.path.insert(0, str(REPO / "engine" / "lib"))

import check_fpga  # noqa: E402
from checklib import CheckError  # noqa: E402

COUNTER8 = REPO / "corpus" / "vde" / "counter8"


def test_settings_absent_means_not_asked():
    assert check_fpga.fpga_settings({"top": "x"}) is None


def test_settings_default_board_and_unknown_board():
    assert check_fpga.fpga_settings({"fpga": {}}) == {"board": "icebreaker"}
    with pytest.raises(CheckError, match="not one of"):
        check_fpga.fpga_settings({"fpga": {"board": "nosuchboard"}})
    with pytest.raises(CheckError, match="mapping"):
        check_fpga.fpga_settings({"fpga": {"board": "icebreaker",
                                           "seed": 3}})


def test_clock_needed():
    assert check_fpga.spec_clock_mhz({"clock": {"period_ns": 20}}) == 50.0
    with pytest.raises(CheckError, match="period_ns"):
        check_fpga.spec_clock_mhz({})


def test_pcf_places_every_board_pin_once():
    text = check_fpga.pcf("icebreaker")
    nets = [ln.split()[2] for ln in text.splitlines()
            if ln.startswith("set_io")]
    assert len(nets) == 2 + 8 * 3 and len(set(nets)) == len(nets)
    pins = [ln.split()[3] for ln in text.splitlines()
            if ln.startswith("set_io")]
    assert len(set(pins)) == len(pins), "two nets on one package pin"


def test_board_top_instantiates_wrapper():
    top = check_fpga.board_top("tt_um_x")
    assert "module fpga_top" in top and "tt_um_x dut" in top
    assert "SB_IO" in top


def make_ws(tmp_path, edit=None) -> Path:
    ws = tmp_path / "ws"
    (ws / "spec").mkdir(parents=True)
    shutil.copytree(COUNTER8 / "rtl", ws / "rtl")
    spec = yaml.safe_load((COUNTER8 / "spec.yaml").read_text())
    if edit:
        edit(spec)
    (ws / "spec" / "spec.yaml").write_text(yaml.safe_dump(spec))
    return ws


def run(ws) -> dict:
    payload, _ = check_fpga.run(["--workspace", str(ws)])
    return payload


def test_no_fpga_section_is_not_applicable_never_a_pass(tmp_path):
    ws = make_ws(tmp_path, lambda s: s.pop("fpga"))
    p = run(ws)
    assert p["applicable"] is False and "not asked for" in p["reason"]
    assert not (ws / "log" / "fpga").exists()
    import attest
    assert attest.not_applicable_reason(ws, "vde", "fpga")
    assert attest.not_applicable_reason(make_ws(tmp_path / "b"), "vde",
                                        "fpga") is None


@pytest.mark.slow
def test_counter8_places_above_its_clock(tmp_path):
    ws = make_ws(tmp_path)
    p = run(ws)
    assert p["status"] == "pass", p["violations"]
    assert 0 < p["luts"] < p["luts_available"]
    assert p["fmax_mhz"] > p["clock_mhz"] == 50.0
    assert p["bitstream"] == "log/fpga/counter8_icebreaker.bin"
    assert (ws / p["bitstream"]).stat().st_size > 0


@pytest.mark.slow
def test_clock_beyond_fmax_fails_by_name(tmp_path):
    ws = make_ws(tmp_path, lambda s: s["clock"].update(period_ns=2))
    p = run(ws)
    assert p["status"] == "violations"
    assert "fpga_fmax_below_clock" in {v["kind"] for v in p["violations"]}


@pytest.mark.slow
def test_comb_loop_fails_by_name(tmp_path):
    ws = make_ws(tmp_path)
    # the loop must feed count, or yosys drops it before the loop check
    v = ws / "rtl" / "counter8.v"
    v.write_text(v.read_text()
                 .replace("  always", "  wire lp;\n  assign lp = lp ^ count[0];"
                          "\n  always", 1)
                 .replace("count + 8'd1;", "(count + 8'd1) ^ {7'd0, lp};"))
    p = run(ws)
    assert p["status"] == "violations"
    assert "fpga_comb_loop" in {v["kind"] for v in p["violations"]}


@pytest.mark.slow
def test_latch_fails_by_name(tmp_path):
    ws = make_ws(tmp_path)
    v = ws / "rtl" / "counter8.v"
    v.write_text(v.read_text().replace(
        "endmodule",
        "  reg lq;\n  always @(*) if (rst) lq = count[0];\nendmodule"))
    p = run(ws)
    assert p["status"] == "violations"
    assert "fpga_latch" in {v["kind"] for v in p["violations"]}
