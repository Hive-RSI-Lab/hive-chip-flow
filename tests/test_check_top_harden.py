"""check_top_harden's macro sizing, without magic or LibreLane; and one
slow real run of the analog tile (corpus/msde/dac_tile)."""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path

import pytest

ENGINE = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import check_top_harden  # noqa: E402
from checklib import CheckError  # noqa: E402

db = pytest.importorskip("klayout.db")


def _gds(tmp_path: Path, width_um: float) -> Path:
    layout = db.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell("mac")
    cell.shapes(layout.layer(34, 0)).insert(
        db.DBox(0, 0, width_um, 1))
    path = tmp_path / "mac.gds"
    layout.write(str(path))
    return path


def test_gds_extent_reads_drawn_geometry(tmp_path):
    assert check_top_harden.gds_extent(_gds(tmp_path, 400), "mac") == (400.0, 1.0)


def test_macro_size_takes_drawn_extent_past_lef(tmp_path):
    lef = tmp_path / "mac.lef"
    lef.write_text("MACRO mac\n  SIZE 45.600 BY 31.040 ;\nEND mac\n")
    # metal drawn past the LEF boundary widens the macro
    assert check_top_harden.macro_size(lef, _gds(tmp_path, 400), "mac") == (400.0, 31.04)
    # geometry inside the boundary leaves the LEF SIZE standing
    assert check_top_harden.macro_size(lef, _gds(tmp_path, 10), "mac") == (45.6, 31.04)


def test_signal_pins_expands_a_bus_to_its_bits():
    pins = check_top_harden.signal_pins({"dac_code": "d2a", "en": "d2a"},
                                        {"dac_code": 8})
    assert pins["en"] == "en"
    assert {p for p, s in pins.items() if s == "dac_code"} == {
        f"dac_code[{i}]" for i in range(8)}
    assert "dac_code" not in pins


def test_centre_fits_and_refuses():
    die = "0 0 346.64 160.72"
    assert check_top_harden.centre(die, 45.6, 31.04) == [150.64, 66.64]
    assert check_top_harden.centre(die, 400.0, 31.04) is None


# the TT analog 1x2 tile, the PLL's
_TILE = "0 0 346.64 325.36"


def _signal_macro(tmp_path: Path, side: str) -> dict:
    """A 122.02 um wide macro abstract, the PLL's width, with its digital
    signal pins (pfd_up, pfd_dn) at its `side` edge."""
    x = 121.32 if side == "right" else 0.3
    lef = tmp_path / f"sig_{side}.lef"
    lef.write_text("MACRO sig\n  SIZE 122.020 BY 296.760 ;\n" + "".join(
        f"  PIN {pin}\n    PORT\n      LAYER Metal3 ;\n"
        f"        RECT {x} {y} {x + 0.4} {y + 0.4} ;\n    END\n  END {pin}\n"
        for pin, y in (("pfd_up", 150), ("pfd_dn", 160)))
        + "END sig\n")
    return {"cell": "sig", "files": {"lef": str(lef)},
            "pins": {"pfd_up": "pfd_up", "pfd_dn": "pfd_dn"}}


def test_place_macro_centres_while_a_row_runs_past_it(tmp_path):
    """The PLL's old 259.46 um macro: centred at y 31.36, its 10 um halo
    leaves rows below it, so the Metal1 rails join the cells either side."""
    macro = _signal_macro(tmp_path, "right")
    loc, note, bounds = check_top_harden.place_macro(
        macro, _TILE, (122.02, 259.46))
    assert loc == [112.56, 31.36] and note == "centred" and bounds is None
    assert check_top_harden.power_split(_TILE, loc, 122.02, 259.46) is None


def test_place_macro_goes_to_the_edge_its_pins_face_away_from(tmp_path,
                                                              capsys):
    """The 296.76 um macro (breakage: PSM-0069 at the harden's step 57):
    centred, it and its halo cover every row (core y 3.92..321.44), so the
    cells right of it were an island; it goes to the edge that keeps its
    signal pins facing the std-cell block, slid no further than its halo
    still covers the strip to that edge, and down to the core's bottom edge
    so full rows above it join its supply stripes to the cell grid."""
    macro = _signal_macro(tmp_path, "right")
    loc, note, bounds = check_top_harden.place_macro(
        macro, _TILE, (122.02, 296.76))
    assert loc == [3.36, 3.92] and bounds == (3.36, 13.36)
    assert note.startswith("at the left edge") and "pfd_up" in note
    assert "bottom edge" in note
    assert "left edge" in capsys.readouterr().err
    assert check_top_harden.power_split(_TILE, loc, 122.02, 296.76) is None
    # the pins on the left put it at the right edge (core x ends at 343.28)
    macro = _signal_macro(tmp_path, "left")
    loc, note, bounds = check_top_harden.place_macro(
        macro, _TILE, (122.02, 296.76))
    assert loc == [221.2, 3.92] and bounds == (211.26, 221.2)
    assert note.startswith("at the right edge")
    assert check_top_harden.power_split(_TILE, loc, 122.02, 296.76) is None


def test_power_split_names_the_island():
    """Centred, the 296.76 um macro leaves cells on both sides and no row
    past it: the narrower block, on its right, is the island."""
    split = check_top_harden.power_split(_TILE, [123.2, 15.68], 122.02,
                                         296.76)
    assert split["island"] == "right"
    assert split["left"] == [3.36, 113.2] and split["right"] == [255.22,
                                                                 343.28]
    assert split["macro_y"] == [5.68, 322.44]


def test_power_split_names_the_macro_island():
    """At the side edge but still covering every row (y 15.68), the cells
    are one block, yet the macro's own stripes reach no row: the macro is
    the island (the top3 harden's PSM-0069)."""
    split = check_top_harden.power_split(_TILE, [3.36, 15.68], 122.02,
                                         296.76)
    assert split["island"] == "macro" and split["macro"] == [3.36, 125.38]
    assert "reach no row" in check_top_harden.split_message(
        {"cell": "sig", "location": [3.36, 15.68]}, split)


def test_row_location_finds_a_full_row():
    """The 296.76 um macro at the bottom edge leaves full rows above it;
    one taller than the core minus a halo and a row leaves none."""
    assert check_top_harden.row_location(_TILE, 296.76, 3.36) == [3.36,
                                                                  3.92]
    assert check_top_harden.row_location(_TILE, 320.0, 3.36) is None


def test_power_location_keeps_to_its_bounds(tmp_path, monkeypatch):
    """The edge window: a stripe reaching the supplies outside it is no
    place for the macro."""
    lef = _power_lef(tmp_path, [])
    macro = {"files": {"lef": str(lef), "gds": "x"}, "cell": "pwr",
             "power": {"vdd": "vdd", "vss": "vss"}}
    stripes = [("VPWR", 50.0), ("VGND", 53.3)]
    monkeypatch.setattr(check_top_harden, "macro_size",
                        lambda *_a: (100.0, 100.0))
    free = check_top_harden.power_location(macro, "0 0 300 200", [0.0, 50.0],
                                           stripes, 0.8, 38.87)
    assert free == [0.0, 50.0]
    assert check_top_harden.power_location(
        macro, "0 0 300 200", [0.0, 50.0], [("VPWR", 120.0),
                                            ("VGND", 123.3)], 0.8, 38.87,
        (0.0, 10.0)) is None


def _lef(tmp_path: Path, vdd_layer: str, vss_layer: str) -> dict:
    lef = tmp_path / "mac.lef"
    lef.write_text("".join(
        f"  PIN {pin}\n    USE {use} ;\n    PORT\n      LAYER {layer} ;\n"
        f"        RECT 0 0 40 1 ;\n    END\n  END {pin}\n"
        for pin, use, layer in (("vdd", "POWER", vdd_layer),
                                ("vss", "GROUND", vss_layer))))
    return {"files": {"lef": str(lef)}, "power": {"vdd": "vdd", "vss": "vss"}}


def test_a_supply_with_no_metal3_is_unreachable(tmp_path):
    # the r2r DAC's shape: vdd on Metal2 only, which no stripe can land on
    assert check_top_harden.unpowered_supplies(
        _lef(tmp_path, "Metal2", "Metal3")) == ["vdd"]
    assert check_top_harden.unpowered_supplies(
        _lef(tmp_path, "Metal3", "Metal3")) == []


def _power_lef(tmp_path: Path, metal4: list[tuple]) -> Path:
    """A 100 x 100 um macro abstract: vdd's Metal3 strap across it at
    y=50, vss's at y=40, and `metal4` as OBS."""
    lef = tmp_path / "pwr.lef"
    lef.write_text("MACRO pwr\n  SIZE 100.000 BY 100.000 ;\n" + "".join(
        f"  PIN {pin}\n    PORT\n      LAYER Metal3 ;\n"
        f"        RECT 0 {y} 100 {y + 0.4} ;\n    END\n  END {pin}\n"
        for pin, y in (("vdd", 50), ("vss", 40))) + "  OBS\n"
        "      LAYER Metal4 ;\n" + "".join(
            f"        RECT {' '.join(map(str, r))} ;\n" for r in metal4)
        + "  END\nEND pwr\n")
    return lef


def test_supply_reach_keeps_the_pdn_halo_off_the_cells_metal4(tmp_path):
    """The PLL's vdd strap (breakage: PDN-0232, a macro grid with no vias):
    OpenROAD cuts the tile's stripe at each Metal4 OBS grown by the 10 um
    PDN halo and the 0.3 um spacing, so a stripe 1.6 um wide reaches the
    strap only where 11.1 um clear of the cell's Metal4 on its way in from
    the top or bottom edge."""
    lef = _power_lef(tmp_path, [(40, 0, 41, 60)])
    reach = check_top_harden.supply_reach(lef, "vdd", 0.8)
    # from below and from above the wire at x=40-41 is in the way
    assert reach == [(0.8, 28.9), (52.1, 99.2)]
    # a wire wholly above the strap, more than the halo away, blocks only
    # the way in from the top
    lef = _power_lef(tmp_path, [(60, 61, 61, 90)])
    reach = check_top_harden.supply_reach(lef, "vdd", 0.8)
    assert reach == [(0.8, 48.9), (0.8, 99.2), (72.1, 99.2)]


def test_power_location_moves_the_macro_until_its_supplies_are_reached(
        tmp_path):
    """Centred, the tile's VPWR stripe lands where the cell's Metal4 keeps
    it off vdd; a few sites to the side both stripes of the pair get in.
    Metal4 over the whole cell is no place at all."""
    gds = _gds(tmp_path, 100)
    # VPWR every 40 um at 20 + 40k, VGND 3.3 um to its right
    stripes = [(n, x + k * 40) for k in range(9)
               for n, x in (("VPWR", 20), ("VGND", 23.3))]
    # free columns for a stripe centre: x 50.8-55 in the cell's frame
    lef = _power_lef(tmp_path, [(0, 0, 39.7, 100), (66.1, 0, 100, 100)])
    macro = {"cell": "mac", "power": {"vdd": "vdd", "vss": "vss"},
             "files": {"lef": str(lef), "gds": str(gds)}}
    die = "0 0 400 100"
    # at x=160 the stripes fall at 60 / 63.3 in the cell, past 55; the
    # nearest site that brings the VPWR at 220 into 50.8-51.7 (its VGND
    # still under 55) is 15 sites right
    assert check_top_harden.power_location(
        macro, die, [160.0, 0.0], stripes, 0.8, 40) == [168.4, 0.0]
    buried = _power_lef(tmp_path, [(0, 0, 100, 100)])
    macro["files"]["lef"] = str(buried)
    assert check_top_harden.power_location(macro, die, [160.0, 0.0],
                                           stripes, 0.8, 40) is None


# ----------------------------------------------- interface.yaml vs. spec.yaml

def _iface_ws(tmp_path: Path, sig_width: int, port_width: int) -> Path:
    """A workspace with just enough for assemble() to reach the width
    check: digital/ and analog/ each carry state.json for nested(), and
    digital/spec/spec.yaml's one port's width may or may not match
    interface.yaml's own width for the same signal."""
    import yaml

    ws = tmp_path / "ws"
    (ws / "digital" / "spec").mkdir(parents=True)
    (ws / "digital" / "rtl").mkdir(parents=True)
    (ws / "analog").mkdir(parents=True)
    ws.joinpath("digital", "state.json").write_text(
        json.dumps({"skill": "vde"}), encoding="utf-8")
    ws.joinpath("analog", "state.json").write_text(
        json.dumps({"skill": "ade", "block": "dac"}), encoding="utf-8")
    ws.joinpath("digital", "spec", "spec.yaml").write_text(
        yaml.safe_dump({"ports": {
            "dac_code": {"dir": "input", "width": port_width}}}),
        encoding="utf-8")
    ws.joinpath("interface.yaml").write_text(
        yaml.safe_dump({"signals": [
            {"name": "dac_code", "direction": "d2a", "width": sig_width}]}),
        encoding="utf-8")
    return ws


def test_assemble_refuses_when_interface_width_does_not_match_spec_port(
        tmp_path):
    ws = _iface_ws(tmp_path, sig_width=8, port_width=4)
    with pytest.raises(CheckError, match="width does not match") as exc:
        check_top_harden.assemble(ws)
    assert "dac_code" in str(exc.value)


def test_assemble_passes_the_width_check_when_widths_match(tmp_path):
    ws = _iface_ws(tmp_path, sig_width=8, port_width=8)
    # past the width check, assemble() moves on to building the analog
    # macro and fails there instead (no generator under analog/) - proves
    # the width check itself does not fire on a genuine match
    with pytest.raises(CheckError, match="no layout generator"):
        check_top_harden.assemble(ws)


# ------------------------------------------------------------ the top's SDC

# the PLL's shape (ece298a-pll blocks/pll): the VCO clock enters the
# standalone harden on ui_in[3] and comes from the analog macro in the top
PLL_DSPEC = {
    "top": "pll", "ports": {
        "clk": {"dir": "input"}, "vco_out": {"dir": "input"},
        "code": {"dir": "output", "width": 2}, "lock": {"dir": "output"}},
    "tt_pins": {"clk": "clk", "vco_out": "ui_in[3]", "code": "uo_out[5:4]",
                "lock": "uo_out[0]"},
    "clock": {"period_ns": 80, "domains": ["clk", "vco_out"],
              "primary": "clk"}}
PLL_MACRO = {"instance": "u_analog",
             "pins": {"vco_out": "vco_out", "code": "code"}}
PLL_SDC = """\
# vco_out on ui_in[3]
create_clock -name vco_out -period 2.7 [get_ports {ui_in[3]}]
create_generated_clock -name pre -source [get_ports {ui_in[3]}] \\
    -divide_by 2 [get_pins u_pre/q]
set_load 0.1 [get_ports uo_out\\[4\\]]
set_input_delay 1 -clock clk [get_ports -quiet {ui_in[2] ui_in[3]}]
set_input_delay 2 -clock clk [get_ports {ui_in[*]}]
"""


def test_crossing_pins_map_each_pad_bit_to_its_macro_pin():
    assert check_top_harden.crossing_pins(PLL_DSPEC, PLL_MACRO) == {
        "ui_in[3]": "u_analog/vco_out",
        "uo_out[4]": "u_analog/code[0]", "uo_out[5]": "u_analog/code[1]"}


def test_pad_signals_keep_their_pad_out_of_the_crossing_rewrite():
    iface = {"signals": [
        {"name": "code", "direction": "d2a", "pad": True},
        {"name": "vco_out", "direction": "a2d", "pad": True},
        {"name": "other", "direction": "d2a"}]}
    pads = check_top_harden.pad_signals(iface)
    assert pads == {"code"}
    assert check_top_harden.crossing_pins(PLL_DSPEC, PLL_MACRO, pads) == {
        "ui_in[3]": "u_analog/vco_out"}


def test_rewrite_moves_crossing_ports_onto_the_macro_pins():
    cross = check_top_harden.crossing_pins(PLL_DSPEC, PLL_MACRO)
    out = check_top_harden.rewrite_crossing_ports(PLL_SDC, cross)
    assert ("create_clock -name vco_out -period 2.7 "
            "[get_pins {u_analog/vco_out}]") in out
    assert "-source [get_pins {u_analog/vco_out}] \\\n" in out
    assert "set_load 0.1 [get_pins {u_analog/code[0]}]" in out
    assert ("[concat [get_ports -quiet {ui_in[2]}] "
            "[get_pins -quiet {u_analog/vco_out}]]") in out
    # a pattern still matches the top's own ports; a comment is a comment
    assert "[get_ports {ui_in[*]}]" in out
    assert "# vco_out on ui_in[3]" in out
    import ttlib
    assert not ttlib.design_sdc_problems(
        {**PLL_DSPEC, "tt_pins": {"clk": "clk"}}, out)


def test_rewrite_refuses_a_crossing_bit_it_cannot_move():
    cross = check_top_harden.crossing_pins(PLL_DSPEC, PLL_MACRO)
    with pytest.raises(CheckError, match=r"ui_in\[3\].*top\.sdc"):
        check_top_harden.rewrite_crossing_ports(
            "set vco ui_in[3]\ncreate_clock -name vco_out -period 2.7 "
            "[get_ports $vco]\n", cross)


def _sdc_ws(tmp_path: Path, digital: str | None, own: str | None) -> Path:
    ws = tmp_path / "ws"
    (ws / "digital" / "harden").mkdir(parents=True)
    if digital is not None:
        (ws / "digital" / "harden" / "constraints.sdc").write_text(digital)
    if own is not None:
        (ws / "top.sdc").write_text(own)
    return ws


def test_top_sdc_takes_the_integrators_own_first(tmp_path):
    ws = _sdc_ws(tmp_path, PLL_SDC, "create_clock -name vco_out x\n")
    assert check_top_harden.top_sdc(ws, PLL_DSPEC, PLL_MACRO) == \
        "create_clock -name vco_out x\n"
    ws = _sdc_ws(tmp_path / "b", PLL_SDC, None)
    got = check_top_harden.top_sdc(ws, PLL_DSPEC, PLL_MACRO)
    assert got.startswith("# Generated by check_top_harden.py")
    assert "[get_pins {u_analog/vco_out}]" in got


def test_top_sdc_refuses_several_clocks_with_no_sdc_and_is_none_for_one(
        tmp_path):
    ws = _sdc_ws(tmp_path, None, None)
    with pytest.raises(CheckError, match="top.sdc"):
        check_top_harden.top_sdc(ws, PLL_DSPEC, PLL_MACRO)
    one = {**PLL_DSPEC, "clock": {"period_ns": 80, "domains": ["clk"]}}
    assert check_top_harden.top_sdc(ws, one, PLL_MACRO) is None


def _assemble_ws(tmp_path: Path, monkeypatch) -> Path:
    """A PLL-shaped msde workspace assemble() can run on, the analog macro
    and its placement faked out."""
    import yaml

    ws = _sdc_ws(tmp_path, PLL_SDC, None)
    (ws / "digital" / "rtl").mkdir()
    (ws / "analog").mkdir()
    ws.joinpath("digital", "state.json").write_text(
        json.dumps({"skill": "vde"}), encoding="utf-8")
    ws.joinpath("analog", "state.json").write_text(
        json.dumps({"skill": "ade", "block": "a"}), encoding="utf-8")
    (ws / "digital" / "spec").mkdir()
    ws.joinpath("digital", "spec", "spec.yaml").write_text(
        yaml.safe_dump(PLL_DSPEC), encoding="utf-8")
    ws.joinpath("interface.yaml").write_text(yaml.safe_dump({"signals": [
        {"name": "vco_out", "direction": "a2d"},
        {"name": "code", "direction": "d2a", "width": 2}]}),
        encoding="utf-8")

    def fake_macro(analog, signals, macros_dir, ua, widths):
        return {"cell": "a", "instance": "u_analog", "orientation": "N",
                "power": {"vdd": "vdd", "vss": "vss"},
                "pins": {s: s for s in sorted(signals)},
                "files": {"gds": "a.gds", "lef": "a.lef", "vh": "a.v",
                          "spice": "a.spice"}}
    monkeypatch.setattr(check_top_harden, "analog_macro", fake_macro)
    monkeypatch.setattr(check_top_harden, "macro_size",
                        lambda lef, gds, cell: (40.0, 40.0))
    monkeypatch.setattr(check_top_harden, "unpowered_supplies",
                        lambda macro: [])
    monkeypatch.setattr(check_top_harden, "power_location",
                        lambda macro, die, start, *_a: start)
    # the lib view's own tests are tests/test_macrolib.py's; with crossing
    # pins and no macro_timing it would be owed (macro_lib_missing)
    monkeypatch.setattr(check_top_harden, "macro_abstract",
                        lambda *_a: None)
    return ws


def test_assemble_writes_the_top_sdc_after_clearing_top(tmp_path,
                                                        monkeypatch):
    """assemble() rmtrees top/ each run, so a hand-written
    top/harden/constraints.sdc never survives; the one it writes itself
    is there afterwards, from the digital side's SDC."""
    ws = _assemble_ws(tmp_path, monkeypatch)
    stale = ws / "top" / "harden" / "constraints.sdc"
    stale.parent.mkdir(parents=True)
    stale.write_text("# by hand\n")
    top, spec = check_top_harden.assemble(ws)
    sdc = (top / "harden" / "constraints.sdc").read_text()
    assert "# by hand" not in sdc
    assert "create_clock -name vco_out -period 2.7 " \
        "[get_pins {u_analog/vco_out}]" in sdc
    assert "vco_out" not in spec["tt_pins"]


# the PLL's digital override: a clock buffer for the clocks it drives out on
# uo_out[0]/[1], and the resizer kept off those pads
PLL_OVERRIDE = {
    "RSZ_DONT_TOUCH_RX": "^(uo_out\\[[01]\\])$",
    "SYNTH_BUFFER_CELL": "gf180mcu_fd_sc_mcu7t5v0__clkbuf_8/I/Z",
}


def test_top_override_carries_the_digital_one_and_the_integrators_wins(
        tmp_path):
    ws = _sdc_ws(tmp_path, None, None)
    assert check_top_harden.top_override(ws) == {}
    (ws / "digital" / "harden" / "config.override.json").write_text(
        json.dumps({"//": "why", **PLL_OVERRIDE}))
    assert check_top_harden.top_override(ws) == PLL_OVERRIDE
    (ws / "top.override.json").write_text(
        json.dumps({"SYNTH_BUFFER_CELL": "x/I/Z"}))
    assert check_top_harden.top_override(ws) == {"SYNTH_BUFFER_CELL": "x/I/Z"}


@pytest.mark.parametrize("rel", ["top.override.json",
                                 "digital/harden/config.override.json"])
def test_top_override_refuses_a_key_the_engine_owns(tmp_path, rel):
    ws = _sdc_ws(tmp_path, None, None)
    (ws / rel).write_text(json.dumps({"CLOCK_PERIOD": 10}))
    with pytest.raises(CheckError, match=re.escape(rel)):
        check_top_harden.top_override(ws)
    (ws / rel).write_text("{not json")
    with pytest.raises(CheckError, match="not valid JSON"):
        check_top_harden.top_override(ws)


def test_assemble_carries_the_digital_override_into_top_harden(
        tmp_path, monkeypatch):
    """top/ is rebuilt every run, so the digital side's override is what
    reaches the top's check_harden (which reads top/harden/
    config.override.json): without it the top synthesises the default
    buffer onto the clocks the digital harden had fixed."""
    ws = _assemble_ws(tmp_path, monkeypatch)
    top, _spec = check_top_harden.assemble(ws)
    assert not (top / "harden" / "config.override.json").exists()
    (ws / "digital" / "harden" / "config.override.json").write_text(
        json.dumps(PLL_OVERRIDE))
    top, _spec = check_top_harden.assemble(ws)
    got = json.loads((top / "harden" / "config.override.json").read_text())
    assert got == PLL_OVERRIDE
    # with no SDC to write, top/harden is still made for the override
    (ws / "digital" / "harden" / "constraints.sdc").unlink()
    monkeypatch.setitem(PLL_DSPEC, "clock",
                        {"period_ns": 80, "domains": ["clk"]})
    import yaml
    ws.joinpath("digital", "spec", "spec.yaml").write_text(
        yaml.safe_dump(PLL_DSPEC), encoding="utf-8")
    top, _spec = check_top_harden.assemble(ws)
    assert not (top / "harden" / "constraints.sdc").exists()
    assert json.loads((top / "harden" / "config.override.json")
                      .read_text()) == PLL_OVERRIDE


# ---------------------------------------------------- the magic Tcl script

def test_tcl_tok_braces_a_name():
    assert check_top_harden.tcl_tok("dac_code[0]") == "{dac_code[0]}"
    assert check_top_harden.tcl_tok("en") == "{en}"


def test_port_lines_braces_every_bus_bit_with_use_and_class():
    # magic reads this script as Tcl - an unbraced bus-bit name like
    # dac_code[0] runs [0] as a nested command, so a braced token is the
    # only way a bit's `use`/`class` lines ever reach the right port.
    sig_pins = check_top_harden.signal_pins(
        {"dac_code": "d2a", "vout": "a2d"}, {"dac_code": 4})
    lines = check_top_harden.port_lines(
        sig_pins, {"dac_code": "d2a", "vout": "a2d"}, {"trim": 2})
    for i in range(4):
        pin = f"dac_code[{i}]"
        assert f"port {{{pin}}} use signal" in lines
        assert f"port {{{pin}}} class input" in lines
    assert "port {vout} use signal" in lines
    assert "port {vout} class output" in lines
    # ua pads are inout, and braced the same way
    assert "port {trim} use signal" in lines
    assert "port {trim} class inout" in lines
    # every line is a single well-formed Tcl word list - no bare `[` or `]`
    # outside a brace pair
    for line in lines:
        assert re.search(r"\{[^{}]+\}", line), line


def _pinned_gds(tmp_path: Path, *, a_label_on_metal: bool = True) -> Path:
    """A cell like the r2r DAC's dac_code[i]: port a's label on a Metal2 pad
    at the left edge, whose Via1 drops onto a Metal1 strip running into the
    cell; an internal net labelled net1; vdd labelled on its Metal3 rail,
    vss on its Metal1 rail with a Via1/Via2 stack up to its Metal3 rail."""
    layout = db.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell("mac")

    def box(layer, *b):
        cell.shapes(layout.layer(layer, 0)).insert(db.DBox(*b))

    def label(layer, text, x, y):
        cell.shapes(layout.layer(layer, 10)).insert(db.DText(text, x, y))
    box(36, 0, 10, 2, 11)                              # a's Metal2 pad
    label(36, "a", 0.5 if a_label_on_metal else 3.5, 10.5)
    box(35, 1.37, 10.37, 1.63, 10.63)                  # its Via1
    box(34, 1, 10, 40, 11)                             # a's internal strip
    box(34, 5, 12, 40, 13)
    label(34, "net1", 20, 12.5)                        # an internal net
    box(42, 0, 18, 40, 20)
    label(42, "vdd", 20, 19)
    box(34, 0, 0, 40, 1)
    label(34, "vss", 20, 0.5)
    box(35, 20, 0.3, 20.26, 0.56)
    box(36, 19.8, 0, 20.5, 1.5)
    box(38, 20, 1, 20.26, 1.26)
    box(42, 0, 0, 40, 2)                               # vss's Metal3 rail
    path = tmp_path / "mac.gds"
    layout.write(str(path))
    return path


PINNED = {"a": False, "vdd": True, "vss": True}


def test_pin_lines_grow_each_port_to_its_metal_and_drop_other_labels(
        tmp_path):
    lines = check_top_harden.pin_lines(_pinned_gds(tmp_path), "mac", PINNED)
    # a's pin is its Metal2 pad, not the Metal1 strip its net runs on
    assert "setlabel box 0.000um 10.000um 2.000um 11.000um" in lines
    assert not any("40.000um 11.000um" in line for line in lines)
    # vdd's label sits on its rail; vss's sits on Metal1 and gains the
    # Metal3 rail of its net as a second port label
    assert "setlabel box 0.000um 18.000um 40.000um 20.000um" in lines
    assert "setlabel box 0.000um 0.000um 40.000um 1.000um" in lines
    i = lines.index("box 0.000um 0.000um 40.000um 2.000um")
    assert lines[i + 1:i + 3] == ["label {vss} c metal3",
                                  "port make [port {vss} index]"]
    assert not any(line.startswith("label {vdd}") for line in lines)
    # net1 names no port: its label is deleted, the label after a port's
    # select is resized instead
    i = lines.index("box 19.995um 12.495um 20.005um 12.505um")
    assert lines[i + 1:i + 3] == ["select area labels", "delete"]
    assert lines.count("delete") == 1


def test_pin_lines_drop_a_label_on_a_metals_drawing_datatype(tmp_path):
    """A generator's label on a metal's drawing datatype (0), not its label
    one (10): magic reads it as a label too, so unless it is deleted
    -pinonly writes it as a pin the macro's interface does not have."""
    gds = _pinned_gds(tmp_path)
    layout = db.Layout()
    layout.read(str(gds))
    cell = layout.cell("mac")
    cell.shapes(layout.layer(34, 0)).insert(db.DText("net2", 30, 12.5))
    cell.shapes(layout.layer(36, 0)).insert(db.DText("a", 0.5, 10.5))
    layout.write(str(gds))
    lines = check_top_harden.pin_lines(gds, "mac", PINNED)
    i = lines.index("box 29.995um 12.495um 30.005um 12.505um")
    assert lines[i + 1:i + 3] == ["select area labels", "delete"]
    assert lines.count("delete") == 2
    # a's label on both datatypes at one point is grown once
    assert lines.count("setlabel box 0.000um 10.000um 2.000um 11.000um") == 1


def test_pin_lines_offer_no_supply_metal3_its_own_metal4_buries(tmp_path):
    """The PLL's vdd via pad (breakage: PDN-0006, "vdd on Metal3 is blocked
    by obstructions on Metal4"): a 0.4 um Metal3 pad of vss's net under the
    Metal4 that joins it to the rail. -pinonly writes that Metal4 as OBS,
    and OpenROAD stops on a pin its OBS covers, so the pad is no pin; the
    rail the same Metal4 crosses only in part still is."""
    gds = _pinned_gds(tmp_path)
    layout = db.Layout()
    layout.read(str(gds))
    cell = layout.cell("mac")
    for layer, b in ((46, (30, 0, 30.4, 5.4)),         # vss's Metal4 jumper
                     (40, (30.07, 0.07, 30.33, 0.33)),  # Via3 to the rail
                     (40, (30.07, 5.07, 30.33, 5.33)),  # Via3 to the pad
                     (42, (30, 5, 30.4, 5.4))):         # the buried pad
        cell.shapes(layout.layer(layer, 0)).insert(db.DBox(*b))
    layout.write(str(gds))
    lines = check_top_harden.pin_lines(gds, "mac", PINNED)
    i = lines.index("box 0.000um 0.000um 40.000um 2.000um")
    assert lines[i + 1] == "label {vss} c metal3"
    assert "box 30.000um 5.000um 30.400um 5.400um" not in lines


def test_pin_lines_keep_a_mim_caps_plates_apart(tmp_path):
    """The PLL's loop-filter cap (breakage: top LVS shorted vss to vctrl):
    a MIM's top plate (FuseTop) sits on its bottom plate's Metal4 and its
    Via4 goes up to vss's Metal5. That Via4 joins the top plate, not the
    Metal4 under it, so the bottom plate's net (vctrl, on a Metal3 pad) is
    no vss pin; the bottom plate's own Via4 column, off the top plate,
    still joins Metal4 to Metal5."""
    gds = _pinned_gds(tmp_path)
    layout = db.Layout()
    layout.read(str(gds))
    cell = layout.cell("mac")
    for layer, b in ((81, (10, 8, 20, 14)),            # vss's Metal5 strap
                     (41, (10.1, 8.1, 10.36, 8.36)),   # Via4 down to ...
                     (46, (10, 0, 10.5, 8.5)),         # ... vss's Metal4
                     (40, (10.1, 0.1, 10.36, 0.36)),   # Via3 to the rail
                     (46, (12, 6, 18, 12)),            # bottom plate
                     (75, (13, 7, 17, 11)),            # top plate, FuseTop
                     (41, (14.87, 8.87, 15.13, 9.13)),  # Via4 on it
                     (40, (12.1, 6.1, 12.36, 6.36)),   # Via3 to vctrl's
                     (42, (11, 6, 12.5, 6.5))):        # Metal3 pad
        cell.shapes(layout.layer(layer, 0)).insert(db.DBox(*b))
    layout.write(str(gds))
    lines = check_top_harden.pin_lines(gds, "mac", PINNED)
    i = lines.index("box 0.000um 0.000um 40.000um 2.000um")
    assert lines[i + 1] == "label {vss} c metal3"
    assert "box 11.000um 6.000um 12.500um 6.500um" not in lines
    # the same pad joined to vss's Metal5 by a Via4 off the top plate is
    # vss's: the cut is FuseTop's, not every Via4 on a bottom plate
    cell.shapes(layout.layer(41, 0)).insert(
        db.DBox(12.1, 11.6, 12.36, 11.86))
    layout.write(str(gds))
    lines = check_top_harden.pin_lines(gds, "mac", PINNED)
    i = lines.index("box 11.000um 6.000um 12.500um 6.500um")
    assert lines[i + 1] == "label {vss} c metal3"


def test_pin_lines_refuse_a_port_label_on_no_metal(tmp_path):
    gds = _pinned_gds(tmp_path, a_label_on_metal=False)
    with pytest.raises(CheckError, match="the label a at .* no Metal2"):
        check_top_harden.pin_lines(gds, "mac", PINNED)


def test_clean_gds_gives_a_cell_with_no_pr_boundary_one(tmp_path):
    """The PLL's layout draws no PR boundary, and LibreLane's
    Magic.StreamOut stopped on it ("Failed to extract PR boundary from
    GDSII view of macro"): the cleaned cell gets one over what it draws,
    from (0, 0); a cell that has one keeps it as it is."""
    layout = db.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell("mac")
    cell.shapes(layout.layer(34, 0)).insert(db.DBox(-1, -2, 10, 20))
    src, dst = tmp_path / "src.gds", tmp_path / "dst.gds"
    layout.write(str(src))
    check_top_harden.clean_gds(src, dst, "mac")
    out = db.Layout()
    out.read(str(dst))
    pr = out.find_layer(*check_top_harden.PR_BOUNDARY)
    assert [s.dbox for s in out.cell("mac").shapes(pr).each()] == [
        db.DBox(0, 0, 11, 22)]

    again = tmp_path / "again.gds"
    out.cell("mac").shapes(pr).clear()
    out.cell("mac").shapes(pr).insert(db.DBox(5, 5, 11, 22))
    out.write(str(dst))
    check_top_harden.clean_gds(dst, again, "mac")
    kept = db.Layout()
    kept.read(str(again))
    pr = kept.find_layer(*check_top_harden.PR_BOUNDARY)
    assert [s.dbox for s in kept.cell("mac").shapes(pr).each()] == [
        db.DBox(5, 5, 11, 22)]


def test_clean_gds_moves_a_cell_drawn_below_its_origin_to_it(tmp_path):
    """The R-2R DAC's vss strap below y=0 (breakage 35): the cleaned cell
    draws from (0, 0), its labels moved with its metal; a cell already
    there is left where it is."""
    layout = db.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell("mac")
    cell.shapes(layout.layer(42, 0)).insert(db.DBox(-2.04, -3.2, 40, -1))
    cell.shapes(layout.layer(34, 0)).insert(db.DBox(0, 0, 10, 136.36))
    cell.shapes(layout.layer(34, 10)).insert(db.DText("a", 5, 5))
    src, dst = tmp_path / "src.gds", tmp_path / "dst.gds"
    layout.write(str(src))
    check_top_harden.clean_gds(src, dst, "mac")
    out = db.Layout()
    out.read(str(dst))
    top = out.cell("mac")
    assert top.dbbox() == db.DBox(0, 0, 42.04, 139.56)
    texts = [s.dtext for s in top.shapes(out.find_layer(34, 10)).each()]
    assert [(t.string, t.x, t.y) for t in texts] == [("a", 7.04, 8.2)]

    at_origin = tmp_path / "ok.gds"
    check_top_harden.clean_gds(dst, at_origin, "mac")
    again = db.Layout()
    again.read(str(at_origin))
    assert again.cell("mac").dbbox() == db.DBox(0, 0, 42.04, 139.56)
    with pytest.raises(CheckError, match="no cell 'nope'"):
        check_top_harden.clean_gds(src, dst, "nope")


def _dcf1a(region_of, box) -> "db.Region":
    """The PDK's DCF.1a (dummy_comp.rb) on one cell placed in a tile: 20 um
    or more with no COMP, COMP_Dummy or keep-out, counting LibreLane's 10
    um halo around the cell as bare and what lies past it as filled."""
    def ring(r, d):
        return r.sized(d * 1000) - r.sized(-d * 1000)
    keep = (region_of(110, 5) + ring(region_of(21, 0), 1)
            + ring(region_of(12, 0), 2))
    return (db.Region(box).sized(10000) - region_of(22, 0)
            - region_of(22, 4) - keep).sized(-10000).sized(10000)


def test_fill_macro_fills_the_comp_gap_and_keeps_off_the_circuit(tmp_path):
    """A macro whose devices sit at one end of a 120 um extent, as the R-2R
    DAC's left an unfilled COMP gap (breakage 37): fill_macro puts
    COMP_Dummy in the gap, so DCF.1a finds nothing, and keeps it off the
    COMP, poly, well edge and resistor marker by the PDK's own spaces and
    inside the macro's extent; it adds no other layer and moves no
    extent."""
    layout = db.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell("mac")
    for spec, box in [((22, 0), (0, 0, 10, 4)), ((30, 0), (4, -1, 5, 5)),
                      ((21, 0), (-2, 10, 12, 20)), ((22, 0), (0, 12, 10, 16)),
                      ((110, 5), (60, 20, 70, 30)),
                      ((34, 0), (0, 0, 120, 1)), ((34, 0), (0, 59, 120, 60))]:
        cell.shapes(layout.layer(*spec)).insert(db.DBox(*box))
    cell.shapes(layout.layer(34, 10)).insert(db.DText("a", 1, 0.5))
    gds = tmp_path / "mac.gds"
    layout.write(str(gds))

    def regions(path):
        ly = db.Layout()
        ly.read(str(path))
        top = ly.cell("mac")

        def region_of(l, d):
            idx = ly.find_layer(l, d)
            return (db.Region() if idx is None
                    else db.Region(top.begin_shapes_rec(idx)).merged())
        return ly, top, region_of

    _ly, before, region_of = regions(gds)
    box = before.bbox()
    assert not _dcf1a(region_of, box).is_empty()  # the gap is there
    kept = {(i.layer, i.datatype): region_of(i.layer, i.datatype)
            for i in _ly.layer_infos()}

    squares = check_top_harden.fill_macro(gds, "mac")
    assert squares > 0
    ly, top, region_of = regions(gds)
    assert [c.name for c in ly.top_cells()] == ["mac"]
    assert top.bbox() == box
    assert _dcf1a(region_of, box).is_empty()
    fill = region_of(22, 4)
    assert fill.count() == squares
    assert {(i.layer, i.datatype) for i in ly.layer_infos()} == {
        *kept, (22, 4)}
    for spec, region in kept.items():
        assert (region_of(*spec) ^ region).is_empty(), spec
    assert fill.space_check(1900).is_empty()                      # DCF.2b
    assert fill.separation_check(region_of(22, 0), 3500).is_empty()  # DCF.4
    assert fill.separation_check(region_of(30, 0), 1500).is_empty()  # DCF.5
    assert fill.separation_check(region_of(21, 0), 1300).is_empty()  # 6a
    assert fill.separation_check(region_of(110, 5), 3500).is_empty()  # 8a
    assert (fill - db.Region(box)).is_empty()
    with pytest.raises(CheckError, match="no cell 'nope'"):
        check_top_harden.fill_macro(gds, "nope")


def test_fill_macro_reaches_a_strip_its_grid_misses(tmp_path):
    """pll_analog's 9 um strip beside its VCO well (DCF.1a at the tile):
    a strip 6 um wide placed off the skewed grid's columns takes no square
    from it, so the fill goes round again on a grid of its own there, and
    keeps the PDK's spaces doing it."""
    layout = db.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell("mac")
    # COMP either side leaves x 13..19 (DCF.4's 3.5 um off each); the
    # skewed grid's columns sit at multiples of 1.6 um, none in 13..14
    for box in ((0, 0, 9.5, 60), (22.5, 0, 60, 60)):
        cell.shapes(layout.layer(22, 0)).insert(db.DBox(*box))
    gds = tmp_path / "mac.gds"
    layout.write(str(gds))
    squares = check_top_harden.fill_macro(gds, "mac")
    ly = db.Layout()
    ly.read(str(gds))
    top = ly.cell("mac")
    fill = db.Region(top.begin_shapes_rec(ly.find_layer(22, 4))).merged()
    comp = db.Region(top.begin_shapes_rec(ly.find_layer(22, 0))).merged()
    assert squares == fill.count() >= 5
    assert (fill - db.Region(db.Box(13000, 0, 19000, 60000))).is_empty()
    assert fill.space_check(1900).is_empty()                      # DCF.2b
    assert fill.separation_check(comp, 3500).is_empty()           # DCF.4


LEF_HEAD = """VERSION 5.7 ;
MACRO tt_um_x
  CLASS BLOCK ;
  SIZE 346.640 BY 325.360 ;
  PIN VGND
    DIRECTION INOUT ;
    USE GROUND ;
    PORT
      LAYER Metal4 ;
        RECT 22.180 3.620 23.780 321.740 ;
    END
    PORT
      LAYER Metal4 ;
        RECT 138.790 3.620 140.390 80.000 ;
    END
    PORT
      LAYER Metal4 ;
        RECT 138.790 210.000 140.390 321.740 ;
    END
  END VGND
  PIN VPWR
    DIRECTION INOUT ;
    USE POWER ;
    PORT
      LAYER Metal4 ;
        RECT 18.180 3.620 19.780 321.740 ;
    END
  END VPWR
  PIN clk
    DIRECTION INPUT ;
    PORT
      LAYER Metal4 ;
        RECT 140.000 324.360 140.300 325.360 ;
    END
  END clk
"""


def test_trim_power_pins_moves_the_ports_the_macro_cuts_to_obs(tmp_path):
    """The tile's VGND stripe over the macro comes out of the PDN as two
    ports, each stopping at the macro's halo, and the precheck fails a
    power port that doesn't run within 10 um of both edges: the two go to
    OBS (the metal stays), the full stripes and the signal pins stay pins,
    and running it again changes nothing."""
    lef = tmp_path / "tt_um_x.lef"
    lef.write_text(LEF_HEAD + """  OBS
      LAYER Metal1 ;
        RECT 0.000 0.000 346.640 325.360 ;
  END
END tt_um_x
END LIBRARY
""", encoding="utf-8")
    moved = check_top_harden.trim_power_pins(lef)
    assert moved == ["VGND Metal4 138.790 3.620 140.390 80.000",
                     "VGND Metal4 138.790 210.000 140.390 321.740"]
    text = lef.read_text(encoding="utf-8")
    vgnd = text[text.index("PIN VGND"):text.index("END VGND")]
    assert "22.180 3.620 23.780 321.740" in vgnd
    assert "138.790" not in vgnd and vgnd.count("PORT") == 1
    obs = text[text.index("  OBS"):]
    assert "RECT 138.790 3.620 140.390 80.000 ;" in obs
    assert "RECT 138.790 210.000 140.390 321.740 ;" in obs
    assert "RECT 0.000 0.000 346.640 325.360 ;" in obs
    assert "PIN clk" in text and "PIN VPWR" in text
    assert check_top_harden.trim_power_pins(lef) == []
    assert lef.read_text(encoding="utf-8") == text


def test_trim_power_pins_writes_an_obs_and_leaves_a_pin_with_no_full_port(
        tmp_path):
    lef = tmp_path / "tt_um_x.lef"
    lef.write_text(LEF_HEAD + "END tt_um_x\nEND LIBRARY\n",
                   encoding="utf-8")
    assert len(check_top_harden.trim_power_pins(lef)) == 2
    text = lef.read_text(encoding="utf-8")
    assert text.endswith("  END clk\n  OBS\n      LAYER Metal4 ;\n"
                         "        RECT 138.790 3.620 140.390 80.000 ;\n"
                         "      LAYER Metal4 ;\n"
                         "        RECT 138.790 210.000 140.390 321.740 ;\n"
                         "  END\nEND tt_um_x\nEND LIBRARY\n")
    # every VGND port short: the pin stays, for the precheck to name
    only_short = LEF_HEAD.replace(
        "RECT 22.180 3.620 23.780 321.740", "RECT 22.180 3.620 23.780 90.0")
    lef.write_text(only_short + "END tt_um_x\nEND LIBRARY\n",
                   encoding="utf-8")
    assert check_top_harden.trim_power_pins(lef) == []


def test_lef_origin_reads_the_macro_origin(tmp_path):
    lef = tmp_path / "mac.lef"
    lef.write_text("MACRO mac\n  ORIGIN 2.040 3.200 ;\n"
                   "  SIZE 210.520 BY 139.560 ;\nEND mac\n")
    assert check_top_harden.lef_origin(lef) == (2.04, 3.2)
    lef.write_text("MACRO mac\n  SIZE 1.000 BY 1.000 ;\nEND mac\n")
    assert check_top_harden.lef_origin(lef) == (0.0, 0.0)


def test_lef_pins_too_small_names_point_and_missing_pins(tmp_path):
    lef = tmp_path / "mac.lef"
    lef.write_text("\n".join([
        "MACRO mac", "  PIN a", "    PORT", "      LAYER Metal2 ;",
        "        RECT 0.000 10.000 2.000 11.000 ;", "    END", "  END a",
        "  PIN b", "    PORT", "      LAYER Metal2 ;",
        "        RECT 0.495 10.495 0.505 10.505 ;", "    END", "  END b",
        "END mac", ""]))
    assert check_top_harden.lef_pins_too_small(lef, ["a", "b", "c"]) == [
        "b", "c"]


@pytest.mark.slow
def test_lef_offers_only_the_metal_under_each_port_label(tmp_path):
    """Against real magic: the abstract's pin for a is its Metal2 pad and
    the Metal1 strip its Via1 lands on is OBS (ece298a's R-2R DAC, where
    the router dropped a Via1 onto that strip); the unported net1 is OBS,
    not a pin; vss keeps its Metal3 rail for the PDN; and the abstract is
    still the drawn extent."""
    lef = check_top_harden.write_lef(_pinned_gds(tmp_path), "mac", [],
                                     PINNED)
    text = lef.read_text(encoding="utf-8")

    def block(start, end):
        m = re.search(rf"^\s*{start}\s*$(.*?)^\s*{end}\s*$", text,
                      re.MULTILINE | re.DOTALL)
        assert m, text
        return m.group(1)
    pin_a = block("PIN a", "END a")
    assert "LAYER Metal1" not in pin_a and "LAYER Via1" not in pin_a
    assert "RECT 0.000 10.000 2.000 11.000 ;" in pin_a
    obs = block("OBS", "END")
    assert "RECT 1.000 10.000 40.000 11.000 ;" in obs
    assert "RECT 1.370 10.370 1.630 10.630 ;" in obs
    assert "RECT 5.000 12.000 40.000 13.000 ;" in obs
    assert "PIN net1" not in text
    assert check_top_harden.lef_pin_layers(lef, "vss") == {"Metal1",
                                                           "Metal3"}
    assert check_top_harden.lef_size(lef) == (40.0, 20.0)
    assert check_top_harden.lef_pins_too_small(lef, list(PINNED)) == []


@pytest.mark.slow
def test_magic_lef_direction_survives_a_bus_bit_pin_name(tmp_path):
    """The regression itself, against real magic (not a fake): a synthetic
    cell with a width-2 bus pin and a scalar pin, run through the SAME
    port_lines()/tcl_tok() script builder analog_macro uses. Before the
    brace fix, magic ran dac_code[0]'s `[0]` as a nested Tcl command
    ("invalid command name" on stderr) and the bit's PIN block came out of
    `lef write` with no DIRECTION/USE at all - proved empirically against
    this same synthetic cell while writing this test."""
    import layoutlib

    cell = "buscell"
    pins = ["dac_code[0]", "dac_code[1]", "en"]
    layout = db.Layout()
    layout.dbu = 0.001
    top = layout.create_cell(cell)
    m1 = layout.layer(34, 0)          # GF180_LAYER["metal1"]
    m1_label = layout.layer(34, 10)   # GF180_LAYER["metal1_label"]
    for i, name in enumerate(pins):
        x0 = i * 2.0
        top.shapes(m1).insert(db.DBox(x0, 0, x0 + 1, 1))
        top.shapes(m1_label).insert(
            db.DText(name, x0 + 0.5, 0.5))
    gds = tmp_path / f"{cell}.gds"
    layout.write(str(gds))

    sig_pins = check_top_harden.signal_pins(
        {"dac_code": "d2a", "en": "a2d"}, {"dac_code": 2})
    lines = check_top_harden.port_lines(
        sig_pins, {"dac_code": "d2a", "en": "a2d"}, {})
    lef_out = tmp_path / f"{cell}.lef"
    script = "\n".join([
        "drc off", "crashbackups disable", "locking disable",
        f"gds read {gds}", f"load {cell}", "select top cell",
        "port makeall", *lines,
        "property LEFclass BLOCK", f"lef write {lef_out}",
        "quit -noprompt", ""])
    proc = layoutlib.run_eda(["magic", "-noconsole", "-dnull"], cwd=tmp_path,
                             timeout=120, stdin_text=script)
    layoutlib.require_ok(proc, "magic lef write")
    assert lef_out.is_file(), (proc.stdout or "")[-1500:]
    lef = lef_out.read_text(encoding="utf-8")
    for pin, want in (("dac_code[0]", "INPUT"), ("dac_code[1]", "INPUT"),
                      ("en", "OUTPUT")):
        m = re.search(
            r"PIN " + re.escape(pin) + r"\s*\n\s*DIRECTION (\w+)", lef)
        assert m, f"{pin}: no DIRECTION in LEF -\n{lef}"
        assert m.group(1) == want


# ------------------------------------------------ the analog tile, for real

# The r2r DAC's macro: 207.82 x 139.56 um drawn from (-2.04, -3.2), but its
# only PR boundary is the column of eight flattened buf_20 cells in one
# corner.
DAC_DRAWN = (-2.04, -3.2, 205.78, 136.36)
DAC_BOUNDARY = (165.52, 0.0, 202.48, 31.36)


@pytest.mark.slow
def test_lef_covers_a_macro_whose_boundary_is_one_flattened_cell(tmp_path):
    """The abstract is what the macro draws, not the corner a flattened
    standard cell's PR boundary marks (breakage 23, PDN-0179); an abstract
    magic leaves short of the drawing is refused."""
    layout = db.Layout()
    layout.dbu = 0.001
    cell = layout.create_cell("mac")
    x0, y0, x1, y1 = DAC_DRAWN
    for box in ((x0, y0, x0 + 1, y1), (x1 - 1, y0, x1, y1)):  # metal1
        cell.shapes(layout.layer(34, 0)).insert(db.DBox(*box))
    cell.shapes(layout.layer(0, 0)).insert(db.DBox(*DAC_BOUNDARY))
    gds = tmp_path / "mac.gds"
    layout.write(str(gds))

    # drawn below and left of (0, 0), magic writes ORIGIN 2.040 3.200,
    # which the final GDS would not honour (breakage 35): refused
    with pytest.raises(CheckError, match="ORIGIN 2.040 3.200"):
        check_top_harden.write_lef(gds, "mac", [], {})
    check_top_harden.clean_gds(gds, gds, "mac")
    lef = check_top_harden.write_lef(gds, "mac", [], {})
    assert check_top_harden.lef_size(lef) == (207.82, 139.56)
    assert check_top_harden.lef_origin(lef) == (0.0, 0.0)

    with pytest.raises(check_top_harden.CheckError, match="smaller than"):
        check_top_harden.write_lef(
            gds, "mac", ["property FIXED_BBOX {0 0 100 100}"], {})


def _macro_lib(ws: Path) -> None:
    """dac_tile declares macro_timing (#191), so top_harden reads the
    macro_lib gate's .lib files; run it as faults.macro_lib_first does."""
    import check_macro_lib
    payload, _ = check_macro_lib.run(["--workspace", str(ws)])
    assert payload["status"] == "pass", payload.get("violations")


@pytest.mark.slow
def test_dac_tile_goes_green_on_the_analog_tile_and_its_faults_go_red(
        tmp_path):
    """corpus/msde/dac_tile end to end on ONE real harden: vout on ua[0]
    hardens on the analog pin template and passes top_lvs and precheck; vout
    moved off the template is a top_harden finding with no harden; a ua pad
    info.yaml claims with nothing wired to it fails precheck. The planted
    faults are the corpus's own (faults/plant_*.py), which faults.py also
    runs, each on its own harden."""
    import yaml

    import check_precheck
    import check_top_lvs
    import faults

    rung = faults.CORPUS / "msde" / "dac_tile"
    off = faults.make_scratch_workspace(tmp_path / "off", rung, "msde",
                                        "dac_tile")
    faults.import_plant(rung, "plant_top_harden.py")(off)
    payload, _ = check_top_harden.run(["--workspace", str(off)])
    assert payload["status"] == "violations"
    assert {v["kind"] for v in payload["violations"]} == {
        "ua_pin_off_template"}
    assert not (off / "top" / "harden").exists()

    ws = faults.make_scratch_workspace(tmp_path / "ok", rung, "msde",
                                       "dac_tile")
    _macro_lib(ws)
    payload, _ = check_top_harden.run(["--workspace", str(ws)])
    assert payload["status"] == "pass", payload.get("violations")
    assert payload["macros"][0].get("ua") == {"vout": 0}
    for gate in (check_top_lvs, check_precheck):
        payload, _ = gate.run(["--workspace", str(ws)])
        assert payload["status"] == "pass", (gate.__name__,
                                             payload.get("violations"))

    spec_path = ws / "top" / "spec" / "spec.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec["macros"][0]["ua"]["unwired"] = 1
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False),
                         encoding="utf-8")
    payload, _ = check_precheck.run(["--workspace", str(ws)])
    assert payload["status"] == "violations"
    assert any(v["kind"] == "precheck_failed" and "ua[1]" in v["msg"]
               for v in payload["violations"]), payload["violations"]


@pytest.mark.slow
def test_a_macro_bounded_like_the_r2r_dac_goes_through_pdn(tmp_path):
    """corpus/msde/dac_tile with its PR boundary only in the lower-right
    quarter, as the r2r DAC's flattened buf_20 column left its own
    (breakage 23). With the abstract taken from that boundary, LibreLane
    put the drawn macro left of and below where top_harden centred it, and
    openroad-generatepdn failed. Now the abstract is the drawn extent, the
    macro sits where the unmodified rung's does, and top_drc, top_lvs and
    precheck pass on it."""
    import check_precheck
    import check_top_drc
    import check_top_lvs
    import faults

    rung = faults.CORPUS / "msde" / "dac_tile"
    ws = faults.make_scratch_workspace(tmp_path, rung, "msde", "dac_tile")
    gen = ws / "analog" / "layout" / "gen_dac_tile_analog.py"
    src = gen.read_text(encoding="utf-8")
    old = ("    layoutlib.rect(shifted, 0.0, 0.0, bb.width(), bb.height(), "
           "PR_BNDRY)\n")
    assert src.count(old) == 1
    gen.write_text(src.replace(old, (
        "    layoutlib.rect(shifted, bb.width() / 2, 0.0, bb.width(), "
        "bb.height() / 2, PR_BNDRY)\n")), encoding="utf-8")

    _macro_lib(ws)
    payload, _ = check_top_harden.run(["--workspace", str(ws)])
    assert payload["status"] == "pass", payload.get("violations")
    macros = ws / "top" / "macros"
    assert check_top_harden.lef_size(macros / "dac_tile_analog.lef") == \
        pytest.approx(check_top_harden.gds_extent(
            macros / "dac_tile_analog.gds", "dac_tile_analog"), abs=1.0)
    for gate in (check_top_drc, check_top_lvs, check_precheck):
        payload, _ = gate.run(["--workspace", str(ws)])
        assert payload["status"] == "pass", (gate.__name__,
                                             payload.get("violations"))


@pytest.mark.slow
def test_a_macro_drawn_off_its_origin_hardens_with_pins_on_their_routes(
        tmp_path):
    """corpus/msde/dac_tile with its whole layout moved 2.04 um left and
    3.2 um down, as the R-2R DAC's vss strap below y=0 left it (breakage
    35). Magic wrote LEF ORIGIN 2.040 3.200, OpenROAD routed to the
    abstract placed at the DEF point, and the final GDS put the cell's
    origin there, so every pin missed its route and top_lvs failed. Now
    the macro is moved to draw from (0, 0) first, and top_lvs passes."""
    import check_top_lvs
    import faults

    rung = faults.CORPUS / "msde" / "dac_tile"
    ws = faults.make_scratch_workspace(tmp_path, rung, "msde", "dac_tile")
    gen = ws / "analog" / "layout" / "gen_dac_tile_analog.py"
    src = gen.read_text(encoding="utf-8")
    old = ("    dx, dy = -bb.left, -bb.bottom\n"
           "    shifted = gf.Component()\n"
           "    shifted.add_ref(top).move((dx, dy))\n"
           "    layoutlib.rect(shifted, 0.0, 0.0, bb.width(), bb.height(), "
           "PR_BNDRY)\n")
    assert src.count(old) == 1
    gen.write_text(src.replace(old, (
        "    dx, dy = -bb.left - 2.04, -bb.bottom - 3.2\n"
        "    shifted = gf.Component()\n"
        "    shifted.add_ref(top).move((dx, dy))\n"
        "    layoutlib.rect(shifted, -2.04, -3.2, bb.width() - 2.04, "
        "bb.height() - 3.2, PR_BNDRY)\n")), encoding="utf-8")

    _macro_lib(ws)
    payload, _ = check_top_harden.run(["--workspace", str(ws)])
    assert payload["status"] == "pass", payload.get("violations")
    macros = ws / "top" / "macros"
    assert check_top_harden.lef_origin(macros / "dac_tile_analog.lef") == (
        0.0, 0.0)
    layout = db.Layout()
    layout.read(str(macros / "dac_tile_analog.gds"))
    box = layout.cell("dac_tile_analog").dbbox()
    assert (box.left, box.bottom) == (0.0, 0.0)
    payload, _ = check_top_lvs.run(["--workspace", str(ws)])
    assert payload["status"] == "pass", payload.get("violations")


@pytest.mark.slow
def test_a_macro_with_an_unfilled_comp_gap_passes_the_dummy_decks_at_top(
        tmp_path):
    """corpus/msde/dac_tile with its vdd and vss rails run 60 um further
    right and a vdd strap 40 um up from there, so the macro draws a COMP gap
    no fill reaches, as ece298a's R-2R DAC did (breakage 37: top_drc and the precheck each
    failed DCF.1a inside the macro, since the top's fill skips it and the
    block's DRC leaves the dummy decks off). Now top_harden fills the
    macro's gap with dummy COMP, and top_drc - dummy decks included -
    top_lvs and the precheck pass, with the LEF still the drawn extent at
    origin 0 0."""
    import check_precheck
    import check_top_drc
    import check_top_lvs
    import faults
    import layout_gen

    rung = faults.CORPUS / "msde" / "dac_tile"
    ws = faults.make_scratch_workspace(tmp_path, rung, "msde", "dac_tile")
    gen = ws / "analog" / "layout" / "gen_dac_tile_analog.py"
    src = gen.read_text(encoding="utf-8")
    old = ("        layoutlib.rect(top, 0.0, y0, x_right, y1, M1)\n"
           "        layoutlib.rect(top, 0.0, y0, x_right, y1, M3)\n")
    assert src.count(old) == 1
    gen.write_text(src.replace(old, (
        "        layoutlib.rect(top, 0.0, y0, x_right + 60.0, y1, M1)\n"
        "        layoutlib.rect(top, 0.0, y0, x_right + 60.0, y1, M3)\n"
        "        if net == \"vdd\":\n"
        "            layoutlib.rect(top, x_right + 60.0 - RAIL, y0, "
        "x_right + 60.0, y1 + 40.0, M3)\n")), encoding="utf-8")

    raw, cell, _abs, _abstract = layout_gen.build(ws / "analog")

    def gap(path: Path) -> "db.Region":
        ly = db.Layout()
        ly.read(str(path))
        top = ly.cell(cell)

        def region_of(l, d):
            idx = ly.find_layer(l, d)
            return (db.Region() if idx is None
                    else db.Region(top.begin_shapes_rec(idx)).merged())
        return _dcf1a(region_of, top.bbox())
    assert not gap(raw).is_empty()  # the gap the top's fill never reaches

    _macro_lib(ws)
    payload, _ = check_top_harden.run(["--workspace", str(ws)])
    assert payload["status"] == "pass", payload.get("violations")
    macros = ws / "top" / "macros"
    assert gap(macros / f"{cell}.gds").is_empty()
    assert check_top_harden.lef_origin(macros / f"{cell}.lef") == (0.0, 0.0)
    assert check_top_harden.lef_size(macros / f"{cell}.lef") == \
        pytest.approx(check_top_harden.gds_extent(
            macros / f"{cell}.gds", cell), abs=1.0)
    for gate in (check_top_drc, check_top_lvs, check_precheck):
        payload, _ = gate.run(["--workspace", str(ws)])
        assert payload["status"] == "pass", (gate.__name__,
                                             payload.get("violations"))


def test_top_harden_hashes_the_sdcs_the_top_is_built_from(tmp_path):
    # editing the integrator's top.sdc or the digital side's design SDC
    # changes the top's clocks, so it must stale top_harden
    import statelib
    imap = statelib.load_map()
    kinds = imap["gate_inputs"]["msde"]["top_harden"]
    assert kinds[0] == "digital_rtl"
    for kind, rel, a, b in (
            ("top_sdc", "top.sdc",
             "create_clock -name a\n", "create_clock -name b\n"),
            ("digital_sdc", "digital/harden/constraints.sdc",
             "create_clock -name a\n", "create_clock -name b\n"),
            # and the override check_top_harden.top_override carries in
            ("top_override", "top.override.json",
             '{"SYNTH_BUFFER_CELL": "a"}', '{"SYNTH_BUFFER_CELL": "b"}'),
            ("digital_override", "digital/harden/config.override.json",
             '{"SYNTH_BUFFER_CELL": "a"}', '{"SYNTH_BUFFER_CELL": "b"}')):
        assert kind in kinds
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(a, encoding="utf-8")
        before = statelib.hash_kind(tmp_path, kind, imap)[1]
        path.write_text(b, encoding="utf-8")
        assert statelib.hash_kind(tmp_path, kind, imap)[1] != before, kind
    assert "top_sdc" in imap["edit_classes"]["msde"]["top_sdc_edit"]["mutates"]


# Real LibreLane output for sensor_counted's top (#191): each run dir holds
# the yosys netlist and the don't-touch lines of the repair steps' logs.
# `renamed` is plant_top_harden_renamed_net.py's: the digital side reads
# osc_out through its own wire, and synthesis kept that wire's name.
DONT_TOUCH = Path(__file__).resolve().parent / "fixtures" / "top_harden_dont_touch"
ANALOG = {"cell": "sensor_counted_analog", "instance": "u_analog",
          "dont_touch_pins": ["osc_out"]}


def _netlist(run: str) -> str:
    return next((DONT_TOUCH / run).glob("*-yosys-synthesis/*.nl.v")).read_text()


@pytest.mark.parametrize("run, net", [
    ("stock", "u_sensor_counted_digital.osc_out"),
    ("renamed", "u_sensor_counted_digital.osc_async")])
def test_the_dont_touch_net_is_the_one_synthesis_put_on_the_pin(run, net):
    resolved, violations = check_top_harden.resolve_dont_touch(
        _netlist(run), [ANALOG])
    assert violations == [] and resolved == {"u_analog": {"osc_out": net}}
    assert check_top_harden.macro_pin_nets(
        _netlist(run), ANALOG["cell"], "u_analog")["osc_en"] == "ui_in[0]"


def test_a_pin_name_guess_misses_the_renamed_net():
    # the rule #191 shipped first: the pin's name behind any prefix
    guess = re.compile(r"^(?:.*[./])?(?:osc_out)$")
    renamed = check_top_harden.resolve_dont_touch(
        _netlist("renamed"), [ANALOG])[0]["u_analog"]["osc_out"]
    assert not guess.search(renamed)


def test_a_pin_with_no_single_net_is_macro_net_unresolved():
    text = _netlist("stock")
    tied = re.sub(r"\.osc_out\([^)]*\)", ".osc_out(1'h0)", text)
    for netlist, macros in ((tied, [ANALOG]),
                            (text, [{**ANALOG, "instance": "u_other"}]),
                            (text, [{**ANALOG, "dont_touch_pins": ["nope"]}])):
        resolved, violations = check_top_harden.resolve_dont_touch(
            netlist, macros)
        assert [v["kind"] for v in violations] == ["macro_net_unresolved"]


def test_every_repair_step_must_report_the_net_dont_touch():
    run = DONT_TOUCH / "stock"
    ok = {**ANALOG, "dont_touch_nets": {
        "osc_out": "u_sensor_counted_digital.osc_out"}}
    assert check_top_harden.dont_touch_unset(run, [ok]) == []
    # a rule naming another net: the run's synthesis disagrees, and neither
    # repair step's log set it
    wrong = {**ANALOG, "dont_touch_nets": {"osc_out": "w_osc_out"}}
    kinds = [v["kind"] for v in check_top_harden.dont_touch_unset(run, [wrong])]
    assert kinds == ["macro_net_unresolved"] + ["macro_net_not_dont_touch"] * 2
    assert check_top_harden.dont_touch_unset(run, [ANALOG]) == []


# ------------------------------------------------------- macro clock proof (#191)

CLOCK_SYNTH = r"""
module top (input clk);
  sensor_counted_analog u_analog (.osc_en(en), .osc_out(\d.osc ));
  buf_cell_placeholder_not_a_buffer _9_ (.A(\d.osc ), .Y(n1));
  dffq _1_ (.CLK(\d.osc ), .D(d1), .Q(q1));
  dffq _2_ (.CLK(\d.osc ), .D(d2), .Q(q2));
  dffq _3_ (.CLK(clk), .D(d3), .Q(q3));
endmodule
""".replace("buf_cell_placeholder_not_a_buffer", "pdk__clkinv_1")
CLOCK_FINAL = r"""
module top (input clk);
  sensor_counted_analog u_analog (.osc_en(en), .osc_out(\d.osc ));
  pdk__antenna ANTENNA_1 (.I(\d.osc ));
  pdk__clkbuf_8 \clkbuf_0_d.osc  (.I(\d.osc ), .Z(clknet_0));
  pdk__clkbuf_8 \clkbuf_1_0__f_d.osc  (.I(clknet_0), .Z(clknet_a));
  pdk__clkbuf_8 \clkbuf_1_1__f_d.osc  (.I(clknet_0), .Z(clknet_b));
  pdk__clkbuf_8 clkload0 (.I(clknet_b));
  pdk__clkinv_1 _9_ (.A(clknet_a), .Y(n1));
  dffq _1_ (.CLK(clknet_a), .D(d1), .Q(q1));
  dffq _2_ (.CLK(clknet_b), .D(d2), .Q(q2));
  dffq _3_ (.CLK(clk), .D(d3), .Q(q3));
endmodule
"""
CLOCK_MACRO = {"cell": "sensor_counted_analog", "instance": "u_analog",
               "clock_pins": ["osc_out"], "clock_nets": {"osc_out": "d.osc"}}
CLOCK_LOG = '[INFO CTS-0007] Net "d.osc" found for clock "osc".\n'


def _clock_run(tmp_path, synth=CLOCK_SYNTH, final=CLOCK_FINAL, log=CLOCK_LOG):
    run = tmp_path / "run"
    (run / "06-yosys-synthesis").mkdir(parents=True)
    (run / "06-yosys-synthesis" / "top.nl.v").write_text(synth)
    if final is not None:
        (run / "final" / "nl").mkdir(parents=True)
        (run / "final" / "nl" / "top.nl.v").write_text(final)
    if log is not None:
        (run / "35-openroad-cts").mkdir()
        (run / "35-openroad-cts" / "openroad-cts.log").write_text(log)
    return run


def _clock_kinds(tmp_path, **kw):
    proof, violations = check_top_harden.macro_clock_proof(
        _clock_run(tmp_path, **kw), [CLOCK_MACRO])
    return proof, [v["kind"] for v in violations]


def test_a_cts_tree_on_a_macro_clock_is_proven_by_name(tmp_path):
    proof, kinds = _clock_kinds(tmp_path)
    assert kinds == []
    assert proof["u_analog/osc_out"]["clock"] == "osc"
    assert proof["u_analog/osc_out"]["sinks"] == 3
    assert len(proof["u_analog/osc_out"]["buffers"]) == 3


@pytest.mark.parametrize("name", [
    r"\clkbuf_regs_0_osc ", r"\delaybuf_0_osc ", r"\clkbuf_0_d.osc_regs ",
    r"\clkbuf_leaf_3_d.osc "])
def test_each_name_cts_gives_a_tree_buffer_is_followed(tmp_path, name):
    final = CLOCK_FINAL.replace(r"\clkbuf_0_d.osc ", name)
    assert _clock_kinds(tmp_path, final=final)[1] == []


def test_an_inverter_cts_cloned_per_sink_is_the_one_synthesis_placed(tmp_path):
    final = CLOCK_FINAL.replace(
        "pdk__clkinv_1 _9_ (.A(clknet_a), .Y(n1));",
        "pdk__clkinv_1 _9_ (.A(clknet_a), .Y(n1));\n"
        "  pdk__clkinv_1 _9___4 (.A(clknet_b), .Y(n2));\n")
    assert _clock_kinds(tmp_path, final=final)[1] == []


def test_a_final_pin_on_another_net_is_macro_clock_net_unresolved(tmp_path):
    final = CLOCK_FINAL.replace(r".osc_out(\d.osc )", ".osc_out(net38)")
    proof, kinds = _clock_kinds(tmp_path, final=final)
    assert proof == {} and kinds == ["macro_clock_net_unresolved"]


def test_the_harden_synthesis_disagreeing_with_the_probe_is_named(tmp_path):
    synth = CLOCK_SYNTH.replace(r"\d.osc ", "other")
    assert _clock_kinds(tmp_path, synth=synth)[1] == [
        "macro_clock_net_unresolved"]


def test_a_cts_log_not_naming_the_net_is_macro_clock_net_unresolved(tmp_path):
    log = '[INFO CTS-0007] Net "clk" found for clock "clk".\n'
    assert _clock_kinds(tmp_path, log=log)[1] == ["macro_clock_net_unresolved"]


def test_a_run_past_cts_with_no_cts_log_proves_nothing(tmp_path):
    assert _clock_kinds(tmp_path, log=None)[1] == ["macro_clock_net_unresolved"]


@pytest.mark.parametrize("old, new", [
    # a buffer no CTS name vouches for, on the way to a sink
    (r"pdk__clkbuf_8 \clkbuf_1_0__f_d.osc  (.I(clknet_0)",
     "pdk__buf_2 u_osc_buffer (.I(clknet_0)"),
    # a repair buffer the resizer named, off the macro's own net
    (r"pdk__clkbuf_8 \clkbuf_0_d.osc  (.I(\d.osc )",
     r"pdk__clkbuf_8 rebuffer12 (.I(\d.osc )"),
    # a CTS name for some other net
    (r"\clkbuf_1_0__f_d.osc ", "clkbuf_1_0__f_clk"),
    # an alias: the net goes on under a name the walk can't follow
    ("  dffq _1_ (.CLK(clknet_a)", "  assign alias = clknet_a;\n  dffq _1_ (.CLK(alias)"),
    # a sink lost off the net
    ("dffq _2_ (.CLK(clknet_b)", "dffq _2_ (.CLK(clk)"),
    # a sink synthesis never put on it
    ("dffq _3_ (.CLK(clk)", "dffq _3_ (.CLK(clknet_b)"),
    # a sink moved to another pin of the same cell
    ("dffq _1_ (.CLK(clknet_a), .D(d1)", "dffq _1_ (.CLK(d1), .D(clknet_a)"),
])
def test_a_clock_path_not_accounted_for_is_macro_clock_buffer_unproven(
        tmp_path, old, new):
    assert old in CLOCK_FINAL
    assert _clock_kinds(tmp_path, final=CLOCK_FINAL.replace(old, new))[1] == [
        "macro_clock_buffer_unproven"]


def test_a_buffer_already_in_synthesis_is_unproven_even_if_gone_after(tmp_path):
    synth = CLOCK_SYNTH.replace(
        r"dffq _2_ (.CLK(\d.osc )",
        "pdk__buf_2 u_b (.I(\\d.osc ), .Z(b));\n  dffq _2_ (.CLK(b)")
    assert _clock_kinds(tmp_path, synth=synth)[1] == [
        "macro_clock_buffer_unproven"]


def test_no_netlist_from_the_flow_leaves_the_flow_to_say_why(tmp_path):
    assert check_top_harden.macro_clock_proof(tmp_path / "none",
                                              [CLOCK_MACRO]) == ({}, [])


def test_a_run_stopped_before_cts_is_walked_as_far_as_it_reached(tmp_path):
    run = _clock_run(tmp_path, final=None, log=None)
    proof, violations = check_top_harden.macro_clock_proof(run, [CLOCK_MACRO])
    assert violations == []
    assert proof["u_analog/osc_out"]["netlist"].startswith("06-yosys")


def test_a_clock_pin_with_no_single_net_is_refused_at_the_probe():
    nets, violations = check_top_harden.resolve_clock_nets(
        CLOCK_SYNTH.replace(r".osc_out(\d.osc )", ".osc_out(1'h0)"),
        [CLOCK_MACRO])
    assert nets == {"u_analog": {}}
    assert [v["kind"] for v in violations] == ["macro_clock_net_unresolved"]


def test_a_clock_pin_resolves_to_the_net_synthesis_put_on_it():
    assert check_top_harden.resolve_clock_nets(CLOCK_SYNTH, [CLOCK_MACRO]) == (
        {"u_analog": {"osc_out": "d.osc"}}, [])


def test_the_pll_shape_one_sink_and_cts_skipping_it_is_proven(tmp_path):
    final = CLOCK_SYNTH.replace(r"dffq _2_ (.CLK(\d.osc )", "dffq _2_ (.CLK(d4)")
    log = CLOCK_LOG + '[WARNING CTS-0041] Net "d.osc" has 1 sinks. Skipping...\n'
    proof, kinds = _clock_kinds(tmp_path, synth=final, final=final, log=log)
    assert kinds == [] and proof["u_analog/osc_out"]["buffers"] == []


# A real run's netlists, trimmed to the clock's statements: sensor_counted with
# osc_out clocking a divider and driving uio_out[1], which synthesis does
# through buffer _214_. CTS split the tree around the macro's own sink.
REAL_CLOCK = Path(__file__).resolve().parent / "fixtures" / "top_harden_macro_clock" / "exported"
REAL_NET = "u_sensor_counted_digital.osc_out"
REAL_MACRO = {"cell": "sensor_counted_analog", "instance": "u_analog",
              "clock_pins": ["osc_out"], "clock_nets": {"osc_out": REAL_NET}}


def _real_clock_run(tmp_path, edit=lambda text: text):
    run = tmp_path / "run"
    shutil.copytree(REAL_CLOCK, run)
    for netlist in run.glob("**/*.nl.v"):
        netlist.write_text(edit(netlist.read_text()))
    return run


def _without_export(text):
    return "".join(line for line in text.splitlines(keepends=True)
                   if " _214_ " not in line)


def test_a_real_run_exporting_the_clock_names_the_synthesis_buffer(tmp_path):
    _, violations = check_top_harden.macro_clock_proof(
        _real_clock_run(tmp_path), [REAL_MACRO])
    assert [v["kind"] for v in violations] == ["macro_clock_buffer_unproven"]
    assert "_214_" in violations[0]["msg"]


def test_the_same_real_tree_with_cts_alone_on_it_is_proven(tmp_path):
    proof, violations = check_top_harden.macro_clock_proof(
        _real_clock_run(tmp_path, _without_export), [REAL_MACRO])
    assert violations == []
    assert proof["u_analog/osc_out"]["clock"] == "osc"
    assert proof["u_analog/osc_out"]["buffers"]


def test_the_same_real_tree_with_the_net_renamed_after_synthesis_is_named(
        tmp_path):
    run = _real_clock_run(tmp_path, _without_export)
    final = next(run.glob("final/nl/*.nl.v"))
    final.write_text(final.read_text().replace(
        f".osc_out(\\{REAL_NET} )", ".osc_out(net38)"))
    _, violations = check_top_harden.macro_clock_proof(run, [REAL_MACRO])
    assert [v["kind"] for v in violations] == ["macro_clock_net_unresolved"]
