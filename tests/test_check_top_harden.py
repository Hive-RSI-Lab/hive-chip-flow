"""check_top_harden's macro sizing, without magic or LibreLane; and one
slow real run of the analog tile (corpus/msde/dac_tile)."""
from __future__ import annotations

import json
import re
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


def test_assemble_writes_the_top_sdc_after_clearing_top(tmp_path,
                                                        monkeypatch):
    """assemble() rmtrees top/ each run, so a hand-written
    top/harden/constraints.sdc never survives; the one it writes itself
    is there afterwards, from the digital side's SDC."""
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
    stale = ws / "top" / "harden" / "constraints.sdc"
    stale.parent.mkdir(parents=True)
    stale.write_text("# by hand\n")

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
    top, spec = check_top_harden.assemble(ws)
    sdc = (top / "harden" / "constraints.sdc").read_text()
    assert "# by hand" not in sdc
    assert "create_clock -name vco_out -period 2.7 " \
        "[get_pins {u_analog/vco_out}]" in sdc
    assert "vco_out" not in spec["tt_pins"]


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
    for kind, rel in (("top_sdc", "top.sdc"),
                      ("digital_sdc", "digital/harden/constraints.sdc")):
        assert kind in kinds
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("create_clock -name a\n", encoding="utf-8")
        before = statelib.hash_kind(tmp_path, kind, imap)[1]
        path.write_text("create_clock -name b\n", encoding="utf-8")
        assert statelib.hash_kind(tmp_path, kind, imap)[1] != before, kind
    assert "top_sdc" in imap["edit_classes"]["msde"]["top_sdc_edit"]["mutates"]
