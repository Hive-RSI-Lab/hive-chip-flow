"""The cosim bench of an analog tile runs through the tile's pad frame
(#208): check_cosim builds the generated TT wrapper around the digital top
(ttlib.write_sim_frame on ttlib.macro_top_spec, the same spec
check_top_harden hardens), with a pin-only model of the analog macro, and
refuses (`cosim_top_unframed`) a bench whose digital top sits outside that
wrapper. These tests cover the pieces without a sim: the spec, the macro
model, the frame files, the vvp scope parse, and the refusal on the
corpus/msde/dac_tile bench as it ships and with its frame taken out. The
end-to-end cases (a bench that ignores the latency fails cosim) are
dac_tile's faults/manifest.yaml plants, run by the harden-faults lane."""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "scripts"))
sys.path.insert(0, str(REPO / "engine" / "lib"))

import check_cosim  # noqa: E402
import faults  # noqa: E402
import ttlib  # noqa: E402

RUNG = REPO / "corpus" / "msde" / "dac_tile"

DSPEC = {
    "top": "blk",
    "clock": {"period_ns": 20, "domains": ["clk"]},
    "ports": {"clk": {"dir": "input"}, "rst_n": {"dir": "input"},
              "code": {"dir": "input", "width": 2},
              "q": {"dir": "output", "width": 2},
              "cmp": {"dir": "input"}},
    "tt_pins": {"clk": "clk", "rst_n": "rst_n", "code": "ui_in[1:0]",
                "q": "uo_out[1:0]", "cmp": "ui_in[2]"},
}
MACRO = {"cell": "cell_a", "instance": ttlib.ANALOG_MACRO_INSTANCE,
         "pins": {"d": "q", "c": "cmp"}, "ua": {"vout": 0}}


def scratch(tmp_path: Path) -> Path:
    return faults.make_scratch_workspace(tmp_path, RUNG, "msde", "dac_tile",
                                         block="dac_tile")


# ------------------------------------------------------------- the spec
def test_macro_top_spec_drops_crossing_pins_and_adds_the_macro():
    tspec = ttlib.macro_top_spec(DSPEC, MACRO, {"q", "cmp"}, "1x2")
    assert tspec["macros"] == [MACRO]
    assert tspec["tiles"] == "1x2"
    assert set(tspec["tt_pins"]) == {"clk", "rst_n", "code"}
    assert set(DSPEC["tt_pins"]) == {"clk", "rst_n", "code", "q", "cmp"}


def test_frame_spec_is_the_tile_check_top_harden_builds(tmp_path):
    spec = check_cosim.frame_spec(scratch(tmp_path))
    (macro,) = spec["macros"]
    assert spec["top"] == "dac_tile_digital"
    assert macro["cell"] == "dac_tile_analog"
    assert macro["instance"] == ttlib.ANALOG_MACRO_INSTANCE
    assert macro["pins"] == {"bmsb": "bmsb", "blsb": "blsb"}
    assert macro["ua"] == {"vout": 0}
    assert not {"bmsb", "blsb"} & set(spec["tt_pins"])
    assert ttlib.pad_reset_latency(spec) == ttlib.PAD_RESET_LATENCY


def test_frame_spec_is_none_without_a_digital_side(tmp_path):
    ws = faults.make_scratch_workspace(tmp_path, REPO / "corpus" / "msde"
                                       / "ring_osc_div", "msde",
                                       "ring_osc_div")
    assert check_cosim.frame_spec(ws) is None


# ---------------------------------------------------- the macro model
def test_macro_sim_model_drives_a2d_pins_and_reads_d2a_pins():
    text = ttlib.generate_macro_sim_model(MACRO, DSPEC)
    assert "module cell_a (" in text
    # q is a block output: the macro reads it
    assert "input  wire [1:0] d" in text
    # cmp is a block input: the macro drives it from its _sim reg
    assert "output wire c" in text
    assert f"reg c{ttlib.MACRO_SIM_DRIVE_SUFFIX};" in text
    assert f"assign c = c{ttlib.MACRO_SIM_DRIVE_SUFFIX};" in text
    assert "inout  wire vout" in text


def test_write_sim_frame_carries_the_macro(tmp_path):
    tspec = ttlib.macro_top_spec(DSPEC, MACRO, {"q", "cmp"})
    files, frame = ttlib.write_sim_frame(tspec, tmp_path)
    assert frame == ttlib.SIM_FRAME_PREFIX + "blk"
    assert [f.name for f in files][1:] == [f"{frame}.v", "cell_a.v"]
    harness = files[1].read_text(encoding="utf-8")
    # the harness keeps the block's own ports, macro-bound ones included
    for port in ("clk", "rst_n", "code", "q", "cmp"):
        assert f" {port}" in harness
    assert f"dut.{ttlib.ANALOG_MACRO_INSTANCE}.c{ttlib.MACRO_SIM_DRIVE_SUFFIX}"\
        " = cmp;" in harness
    assert "assign q = dut.w_q;" in harness


def test_write_sim_frame_is_none_at_latency_zero(tmp_path):
    tspec = ttlib.macro_top_spec(
        dict(DSPEC, pad_reset_sync={"mode": "off", "reason": "test"}),
        MACRO, {"q", "cmp"})
    assert ttlib.pad_reset_latency(tspec) == 0
    assert ttlib.write_sim_frame(tspec, tmp_path) is None


# ------------------------------------------------- the hierarchy check
VVP = '''\
S_0x1 .scope module, "top" "top" 2 3;
S_0x2 .scope module, "digital" "tt_frame_blk" 3 4, 5 6 0, S_0x1;
S_0x3 .scope module, "dut" "tt_um_blk" 4 5, 6 7 0, S_0x2;
S_0x4 .scope module, "u_blk" "blk" 5 6, 7 8 0, S_0x3;
S_0x5 .scope module, "stray" "blk" 5 6, 7 8 0, S_0x1;
'''


def test_scope_parents_reads_each_module_scope_and_its_parent():
    assert check_cosim.scope_parents(VVP) == [
        ("top", "top", None),
        ("digital", "tt_frame_blk", "top"),
        ("dut", "tt_um_blk", "tt_frame_blk"),
        ("u_blk", "blk", "tt_um_blk"),
        ("stray", "blk", "top"),
    ]


def test_unframed_names_every_instance_outside_the_wrapper():
    scopes = check_cosim.scope_parents(VVP)
    assert check_cosim.unframed(scopes, "blk", "tt_um_blk") == [
        "stray (blk) sits in top, not in tt_um_blk"]
    assert check_cosim.unframed(scopes[:4], "blk", "tt_um_blk") == []
    assert check_cosim.unframed(scopes[:3], "blk", "tt_um_blk") == [
        "no instance of blk at all"]


def bench_sources(ws: Path, build: Path):
    spec = check_cosim.frame_spec(ws)
    files, _frame = ttlib.write_sim_frame(spec, build / "frame")
    return spec, [ws / "tb" / "dac_tile_cosim_top.v",
                  ws / "tb" / "dac_tile_digital.v", *files]


@pytest.mark.skipif(not shutil.which("iverilog"),
                    reason="needs the eda image's iverilog")
def test_dac_tile_bench_is_framed(tmp_path):
    ws = scratch(tmp_path)
    spec, sources = bench_sources(ws, tmp_path / "build")
    check_cosim.check_framed(spec, sources, "dac_tile_cosim_top",
                             tmp_path / "build")


@pytest.mark.skipif(not shutil.which("iverilog"),
                    reason="needs the eda image's iverilog")
def test_dac_tile_bench_without_its_frame_is_refused(tmp_path):
    ws = scratch(tmp_path)
    top = ws / "tb" / "dac_tile_cosim_top.v"
    text = top.read_text(encoding="utf-8")
    assert "tt_frame_dac_tile_digital digital (" in text
    top.write_text(text.replace("tt_frame_dac_tile_digital digital (",
                                "dac_tile_digital digital ("),
                   encoding="utf-8")
    spec, sources = bench_sources(ws, tmp_path / "build")
    with pytest.raises(check_cosim.CheckError,
                       match=r"^cosim_top_unframed: digital \(dac_tile_digital"
                             r"\) sits in dac_tile_cosim_top"):
        check_cosim.check_framed(spec, sources, "dac_tile_cosim_top",
                                 tmp_path / "build")
