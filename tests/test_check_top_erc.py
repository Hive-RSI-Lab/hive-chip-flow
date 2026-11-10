"""engine/scripts/check_top_erc.py: msde's top_erc gate (CVC on the flat
transistor netlist of the assembled top, GitHub issue 111).

Fast tests read a tiny powered netlist through the flattener, with cvc_rv
stubbed where a run is needed. Slow tests run cvc_rv for real on a workspace
with a one-inverter standard-cell library in a fake PDK: a clean macro passes,
an unconnected macro input floats, a 3.3 V device on a 5 V supply is over its
rating, and an always-on stack leaks. The planted fault on the real hardened
tile runs in test_corpus_faults.py."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import check_analog_erc as erc  # noqa: E402
import check_lvs  # noqa: E402
import check_top_erc as top_erc  # noqa: E402
import ttlib  # noqa: E402
from checklib import CheckError  # noqa: E402

CELLS = """\
.SUBCKT inv_1 I Z VDD VNW VPW VSS
X_M1 Z I VSS VPW nfet_05v0 W=7.1e-07 L=0.6U
X_M2 Z I VDD VNW pfet_05v0 W=1.2e-06 L=5e-07
.ENDS
.SUBCKT fill_1 VDD VNW VPW VSS
.ENDS
"""
MACRO = """\
.subckt amp en out vdd vss
xn out en vss vss nfet_03v3 w=2e-6 l=1e-6
xp out en vdd vdd pfet_03v3 w=4e-6 l=1e-6
.ends amp
"""
POWERED = """\
module tt_um_x (clk, rst_n, VPWR, VGND, ui_in, uo_out);
  input clk;
  input rst_n;
  inout VPWR;
  inout VGND;
  input [1:0] ui_in;
  output [1:0] uo_out;
  wire net1;
  wire \\u_d.out ;
  inv_1 u1 (.I(ui_in[0]), .Z(net1), .VDD(VPWR), .VNW(VPWR), .VPW(VGND), .VSS(VGND));
  amp u_analog (.en(net1), .out(\\u_d.out ), .vdd(VPWR), .vss(VGND));
  fill_1 f0 (.VDD(VPWR), .VNW(VPWR), .VPW(VGND), .VSS(VGND));
  assign uo_out[0] = \\u_d.out ;
  assign uo_out[1] = \\u_d.out ;
endmodule
"""


def _library():
    lib = top_erc.parse_subckts(CELLS)
    lib.update(top_erc.parse_subckts(MACRO))
    return lib


def _flat(text=POWERED):
    design = top_erc.parse_powered_netlist(text)
    return design, *top_erc.flatten(design, _library(), "VGND")


def _spec():
    return {"macros": [{"cell": "amp", "instance": "u_analog",
                        "power": {"vdd": "vdd", "vss": "vss"},
                        "pins": {"en": "en", "out": "out"}}]}


INTERFACE = {"signals": [{"name": "en", "direction": "d2a"},
                         {"name": "out", "direction": "a2d"}]}


def test_parser_expands_buses_and_unescapes_names():
    design = top_erc.parse_powered_netlist(POWERED)
    assert design["top"] == "tt_um_x"
    assert design["ports"]["ui_in[1]"] == "input"
    assert design["ports"]["uo_out[0]"] == "output"
    assert ("uo_out[0]", "u_d.out") in design["assigns"]
    assert [i[1] for i in design["instances"]] == ["u1", "u_analog", "f0"]


def test_flatten_reaches_every_device_and_merges_assigns():
    _design, flat, paths = _flat()
    body = flat.splitlines()
    assert len(body) == 6, flat       # .subckt, 2 + 2 devices, .ends
    assert sorted(paths.values()) == ["u1/X_M1", "u1/X_M2",
                                      "u_analog/xn", "u_analog/xp"]
    # the assign names the macro output for the top port it drives
    assert any(" uo_out[" in line and "nfet_03v3" in line for line in body)
    # CVC reads a lower-case unit suffix only
    assert "l=0.6u" in flat and "0.6U" not in flat


def test_flat_netlist_is_what_to_cdl_reads():
    design, flat, _paths = _flat()
    devices, ports, _cdl = erc.to_cdl(flat, design["top"])
    assert len(devices) == 4 and "VPWR" in ports


def test_an_open_macro_pin_is_its_own_net():
    _d, flat, _p = _flat(POWERED.replace(".en(net1)", ".en()"))
    assert "u_analog.en" in flat
    _d, flat, _p = _flat(POWERED.replace(".en(net1), ", ""))
    assert "u_analog.en" in flat


def test_a_concatenated_bus_pin_binds_msb_first():
    lib = top_erc.parse_subckts(".subckt dac b[1] b[0] vss\n"
                                "xn b[1] b[0] vss vss nfet_03v3 w=1e-6 l=1e-6\n"
                                ".ends\n")
    text = ("module t (VGND, a, c);\n inout VGND;\n input a;\n input c;\n"
            " dac u (.b({a, c}), .vss(VGND));\nendmodule\n")
    design = top_erc.parse_powered_netlist(text)
    flat, _ = top_erc.flatten(design, lib, "VGND")
    assert "a c VGND VGND nfet_03v3" in flat


def test_spice_ground_is_the_ground_net():
    lib = top_erc.parse_subckts(".subckt c a\nxn a a 0 0 nfet_03v3 w=1e-6 l=1e-6\n.ends\n")
    design = top_erc.parse_powered_netlist(
        "module t (VGND, a);\n inout VGND;\n input a;\n c u (.a(a));\nendmodule\n")
    flat, _ = top_erc.flatten(design, lib, "VGND")
    assert "a a VGND VGND nfet_03v3" in flat


@pytest.mark.parametrize("edit, match", [
    (lambda t: t.replace("inv_1 u1", "nosuch_1 u1"), "neither"),
    (lambda t: t.replace(".en(net1)", ".nosuch(net1)"), "does not have"),
    (lambda t: t.replace(".en(net1)", ".en(1'b1)"), "constant"),
    (lambda t: t.replace("assign uo_out[1] = \\u_d.out ;",
                         "assign uo_out[1] = 1'b0;"), "constant"),
    (lambda t: t.replace("  wire net1;", "  always @(posedge clk) x <= y;"),
     "cannot read"),
])
def test_what_it_cannot_place_refuses(edit, match):
    with pytest.raises(CheckError, match=match):
        _flat(edit(POWERED))


def test_an_unrated_device_or_stray_element_refuses():
    lib = top_erc.parse_subckts(".subckt c a\nR1 a 0 10k\n.ends\n")
    design = top_erc.parse_powered_netlist(
        "module t (VGND, a);\n inout VGND;\n input a;\n c u (.a(a));\nendmodule\n")
    with pytest.raises(CheckError, match="smaller circuit"):
        top_erc.flatten(design, lib, "VGND")
    lib = top_erc.parse_subckts(
        ".subckt c a\nxn a a 0 0 mystery_fet w=1e-6 l=1e-6\n.ends\n")
    with pytest.raises(CheckError, match="neither a PDK device"):
        top_erc.flatten(design, lib, "VGND")


def test_macro_sizing_on_param_lines_reaches_the_device():
    lib = top_erc.parse_subckts(
        ".param r_len=2e-05 W_A=4e-06\n"
        ".subckt c a w_b=1e-06\n"
        "xr a b 0 ppolyf_u r_width=1e-6 r_length=r_len\n"
        "xn a b 0 0 nfet_03v3 w={W_A} l=w_b\n"
        ".ends\n")
    design = top_erc.parse_powered_netlist(
        "module t (VGND, a);\n inout VGND;\n input a;\n c u (.a(a));\nendmodule\n")
    flat, _ = top_erc.flatten(design, lib, "VGND")
    assert "r_length=2e-05" in flat
    assert "w=4e-06 l=1e-06" in flat


def test_a_parameter_with_no_value_refuses():
    lib = top_erc.parse_subckts(
        ".subckt c a\nxn a a 0 0 nfet_03v3 w=wmissing l=1e-6\n.ends\n")
    design = top_erc.parse_powered_netlist(
        "module t (VGND, a);\n inout VGND;\n input a;\n c u (.a(a));\nendmodule\n")
    with pytest.raises(CheckError, match="not a number"):
        top_erc.flatten(design, lib, "VGND")


def test_rails_and_inputs_get_voltages_outputs_do_not():
    design = top_erc.parse_powered_netlist(POWERED)
    pins = top_erc.rail_voltages(design["ports"], 3.3)
    assert pins["VPWR"] == {"power": 3.3} and pins["VGND"] == {"power": 0.0}
    assert pins["ui_in[0]"] == {"min": 0.0, "max": 3.3}
    assert "uo_out[0]" not in pins


def test_a_top_with_no_rail_refuses():
    with pytest.raises(CheckError, match="ground"):
        top_erc.rail_voltages({"VPWR": "inout"}, 3.3)
    with pytest.raises(CheckError, match="supply"):
        top_erc.rail_voltages({"VGND": "inout"}, 3.3)


def test_held_run_cuts_the_crossing_pin_from_its_driver():
    design = top_erc.parse_powered_netlist(POWERED)
    held, nets = top_erc.hold_crossings(design, _spec(), INTERFACE, _library())
    assert nets == ["u_analog.en"]
    flat, _ = top_erc.flatten(held, _library(), "VGND")
    assert "u_analog.en" in flat
    # the digital driver keeps its own net, and the original is untouched
    assert " net1 " in flat
    assert dict((i[1], i[2]) for i in design["instances"])["u_analog"]["en"] == "net1"


def test_a_d2a_signal_bound_to_no_pin_refuses():
    design = top_erc.parse_powered_netlist(POWERED)
    iface = {"signals": [{"name": "lost", "direction": "d2a"}]}
    with pytest.raises(CheckError, match="bound"):
        top_erc.hold_crossings(design, _spec(), iface, _library())


# ------------------------------------------------------------------ a workspace

def _workspace(tmp_path, monkeypatch, pnl=POWERED, macro=MACRO, vdd=3.3):
    ws = tmp_path / "blk"
    top = ws / "top"
    (top / "spec").mkdir(parents=True)
    (top / "macros").mkdir()
    (top / "macros" / "amp.spice").write_text(macro)
    spec = {"name": "x", "macros": [{**_spec()["macros"][0],
                                     "files": {"spice": "macros/amp.spice"}}]}
    (top / "spec" / "spec.yaml").write_text(yaml.safe_dump(spec))
    final = top / "harden" / "runs" / "run" / "final" / "pnl"
    final.mkdir(parents=True)
    (final / "tt_um_x.pnl.v").write_text(pnl)
    (ws / "analog" / "spec").mkdir(parents=True)
    (ws / "analog" / "spec" / "spec.yaml").write_text(
        yaml.safe_dump({"supply": {"vdd": vdd}}))
    (ws / "interface.yaml").write_text(yaml.safe_dump(INTERFACE))
    pdk = tmp_path / "pdk"
    cells = pdk / ttlib.PDK_NAME / check_lvs.PDK_SPICE_MODELS[0]
    cells.parent.mkdir(parents=True)
    cells.write_text(CELLS)
    monkeypatch.setattr(check_lvs, "_pdk_root", lambda: pdk)
    monkeypatch.setattr(top_erc.speclib, "load_spec",
                        lambda p: yaml.safe_load(Path(p).read_text()))
    monkeypatch.setattr(top_erc.ttlib, "wrapper_name", lambda s: "tt_um_x")
    return ws


def _run(ws, tmp_path):
    out = tmp_path / "out.json"
    code = top_erc.main(["--workspace", str(ws), "--out", str(out)])
    return code, json.loads(out.read_text())


def _refusal(ws, capsys):
    code = top_erc.main(["--workspace", str(ws)])
    return code, json.loads(capsys.readouterr().out)


def test_no_hardened_top_is_a_refusal(tmp_path, capsys):
    ws = tmp_path / "blk"
    ws.mkdir()
    assert top_erc.main(["--workspace", str(ws)]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert "top_harden" in payload["remediation"]


def test_missing_netlist_is_a_refusal(tmp_path, monkeypatch, capsys):
    ws = _workspace(tmp_path, monkeypatch)
    next(ws.glob("top/harden/runs/run/final/pnl/*.v")).unlink()
    code, payload = _refusal(ws, capsys)
    assert code == 2 and "powered netlist" in payload["error"]


def test_a_cvc_that_dies_is_a_refusal_not_a_pass(tmp_path, monkeypatch, capsys):
    ws = _workspace(tmp_path, monkeypatch)
    def dead(*a, **k):
        raise CheckError("cvc_rv wrote no error-count summary")
    monkeypatch.setattr(erc, "run_cvc", dead)
    code, payload = _refusal(ws, capsys)
    assert code == 2 and "error-count" in payload["error"]


def test_runs_all_three_modes_with_the_right_power(tmp_path, monkeypatch):
    ws = _workspace(tmp_path, monkeypatch)
    calls = {}
    def fake(work, top, cdl, models, power, timeout=300.0):
        calls[Path(work).name] = (top, power)
        return ("CVC: Error Counts\nCVC: Fuse Errors : 0\n", "")
    monkeypatch.setattr(erc, "run_cvc", fake)
    code, payload = _run(ws, tmp_path)
    assert code == 0 and payload["status"] == "pass"
    assert set(calls) == {"operating", "held", "analog_amp"}
    assert "ui_in[0] input min@0 max@3.3" in calls["operating"][1]
    assert "/u_analog.en" in calls["held"][1]
    assert "/u_analog.en" not in calls["operating"][1]
    held = calls["held"][1]
    assert any(line.startswith("rst_n") and "input" not in line
               for line in held.splitlines())
    assert calls["analog_amp"][0] == "amp"
    assert set(payload["modes"]) == {"operating", "held", "analog:amp"}


def _released_ws(tmp_path, name):
    ws = tmp_path / "blocks" / name
    ws.mkdir(parents=True)
    (ws / "interface.yaml").write_text("version: 1\nsignals: []\n")
    return ws


def test_only_the_named_v1_blocks_do_not_owe_the_gate(tmp_path):
    import attest
    assert "top_erc" not in attest.owed_gates(_released_ws(tmp_path, "pll"), "msde")
    assert "top_erc" in attest.owed_gates(_released_ws(tmp_path, "sensor"), "msde")


# ------------------------------------------------------------------ real CVC

@pytest.mark.slow
def test_clean_macro_passes(tmp_path, monkeypatch):
    code, payload = _run(_workspace(tmp_path, monkeypatch), tmp_path)
    assert code == 0 and payload["status"] == "pass", payload


@pytest.mark.slow
def test_unconnected_macro_input_is_a_floating_gate(tmp_path, monkeypatch):
    pnl = POWERED.replace(".en(net1)", ".en()")
    code, payload = _run(_workspace(tmp_path, monkeypatch, pnl=pnl), tmp_path)
    assert code == 1
    found = [v for v in payload["violations"]
             if v["kind"] == "floating_gate" and v["severity"] == "error"]
    assert found and all(v["mode"] == "operating" for v in found), payload


@pytest.mark.slow
def test_3v3_device_on_a_5v_supply_is_overvoltage(tmp_path, monkeypatch):
    code, payload = _run(_workspace(tmp_path, monkeypatch, vdd=5.0), tmp_path)
    assert code == 1
    kinds = {v["kind"] for v in payload["violations"]}
    assert any(k.startswith("overvoltage") for k in kinds), kinds


@pytest.mark.slow
def test_an_always_on_stack_between_the_rails_is_an_error(tmp_path, monkeypatch):
    macro = MACRO.replace(".ends amp", ("xs1 s vdd vss vss nfet_03v3 w=1e-6 l=1e-6\n"
                                          "xs2 s vss vdd vdd pfet_03v3 w=1e-6 l=1e-6\n"
                                          ".ends amp"))
    code, payload = _run(_workspace(tmp_path, monkeypatch, macro=macro), tmp_path)
    assert code == 1
    assert any(v["kind"] == "leak" and v["severity"] == "error"
               for v in payload["violations"]), payload
