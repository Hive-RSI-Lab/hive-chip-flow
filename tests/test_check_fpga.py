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


# --- #378: the ice40 netlist proved equal to the RTL -----------------------

def test_unproven_names_outputs_first():
    log = ("  Unproven $equiv $auto$equiv_make.cc:295:x$1: "
           "\\dut.count_gold [4] \\dut.count_gate [4]\n"
           "  Unproven $equiv $auto$equiv_make.cc:258:x$2: "
           "\\uo_out_gold [4] \\uo_out_gate [4]\n"
           "  Unproven $equiv $auto$equiv_make.cc:258:x$3: "
           "\\done_gold \\done_gate\n")
    assert check_fpga.unproven(log) == ["done", "uo_out[4]", "dut.count[4]"]
    assert check_fpga.unproven("Of those cells 9 are proven") == []


BLOCK_COUNTER = REPO / "blocks" / "counter"


def block_ws(tmp_path) -> Path:
    ws = tmp_path / "bc"
    (ws / "spec").mkdir(parents=True)
    shutil.copytree(BLOCK_COUNTER / "rtl", ws / "rtl")
    shutil.copy(BLOCK_COUNTER / "spec" / "spec.yaml", ws / "spec")
    return ws


def flip_lut(ws: Path) -> Path:
    """The synthesised netlist with one LUT's init inverted: an edit made
    after synth, which nothing in the flow would make."""
    import json
    out = ws / "log" / "fpga"
    nl = json.loads((out / "fpga_top.json").read_text())
    cells = nl["modules"]["fpga_top"]["cells"]
    # a LUT of the design's own logic: its src points into rtl/ (cell names
    # change between yosys versions: 0.69 named them after `count`, 0.70
    # after the flop they feed)
    name = sorted(n for n, c in cells.items() if c["type"] == "SB_LUT4"
                  and "rtl/" in c.get("attributes", {}).get("src", ""))[0]
    init = cells[name]["parameters"]["LUT_INIT"]
    cells[name]["parameters"]["LUT_INIT"] = init.translate(
        str.maketrans("01", "10"))
    bad = out / "edited.json"
    bad.write_text(json.dumps(nl))
    return bad


def reads_of(ws: Path) -> str:
    ys = (ws / "log" / "fpga" / "synth_ice40.ys").read_text()
    return ys.split("synth_ice40 -top")[0]


@pytest.mark.slow
@pytest.mark.parametrize("which", ["counter8", "blocks/counter"])
def test_netlist_proves_equal_to_rtl(tmp_path, which):
    ws = make_ws(tmp_path) if which == "counter8" else block_ws(tmp_path)
    p = run(ws)
    assert p["status"] == "pass", p["violations"]
    assert p["equiv"]["verdict"] == "equal", p["equiv"]
    assert p["equiv"]["seconds"] < p["equiv"]["budget_s"]
    assert p["netlist_proof"].startswith("equal by ")


@pytest.mark.slow
def test_flipped_lut_init_fails_by_name(tmp_path):
    ws = make_ws(tmp_path)
    assert run(ws)["status"] == "pass"
    bad = flip_lut(ws)
    fact, found = check_fpga.prove_netlist(ws, ws / "log" / "fpga",
                                           reads_of(ws), bad)
    assert fact["verdict"] == "different"
    assert [v["kind"] for v in found] == ["fpga_netlist_not_equal"]
    assert any(r.startswith("uo_out[") for r in found[0]["refs"]), found
    assert found[0]["refs"][0] in found[0]["msg"]


@pytest.mark.slow
def test_out_of_budget_is_unproven_never_a_pass(tmp_path):
    ws = make_ws(tmp_path)
    run(ws)
    bad = flip_lut(ws)
    fact, found = check_fpga.prove_netlist(
        ws, ws / "log" / "fpga", reads_of(ws), bad,
        budget=check_fpga.PDR_MIN_S / 2)
    assert fact["verdict"] == "unproven"
    assert [v["kind"] for v in found] == ["fpga_equiv_unproven"]


# yosys's own latch lines, as 0.69 and 0.70 print them: 0.69 warns and goes
# on, 0.70 stops synth_ice40 with an ERROR; both name the latch
LATCH_069 = ("Warning: Latch inferred for signal `\\counter8.\\lq' from process "
             "`\\counter8.$proc$rtl/counter8.v:13$3': "
             "$auto$proc_dlatch.cc:547:proc_dlatch$404\n")
LATCH_070 = ("ERROR: Latch inferred for signal `\\counter8.\\lq' from process "
             "`\\counter8.$proc$rtl/counter8.v:13$3': "
             "$auto$proc_dlatch.cc:559:proc_dlatch$404\n")


@pytest.mark.parametrize("text", [LATCH_069, LATCH_070],
                         ids=["yosys-0.69", "yosys-0.70"])
def test_inferred_latches_reads_both_yosys_texts(text):
    assert check_fpga.inferred_latches("noise\n" + text) == ["counter8.lq"]


def test_inferred_latches_is_empty_on_a_clean_log():
    assert check_fpga.inferred_latches("End of script.\n") == []
