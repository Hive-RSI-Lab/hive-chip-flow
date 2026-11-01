"""engine/scripts/check_cdc.py and cdclib: real yosys and OpenSTA on small
designs, then the PLL digital's known crossings as the known answer."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "scripts"))
sys.path.insert(0, str(REPO / "engine" / "lib"))

import check_cdc  # noqa: E402
import checklib  # noqa: E402

TWO_CLOCKS = {"period_ns": 20, "domains": ["clk", "clk2"], "primary": "clk"}

# `src` in clk2, sampled in clk. {SYNC} is what sits between.
SOURCE = """\
module top (input wire clk, input wire clk2, input wire a, output wire y);
  reg src, s1, s2, other;
  always @(posedge clk2) src <= ~src;
  always @(posedge clk) other <= a;
  {sync}
  assign y = s2;
endmodule
"""
TWO_FLOP = "always @(posedge clk) begin s1 <= src; s2 <= s1; end"
ONE_FLOP = "always @(posedge clk) begin s1 <= src; s2 <= s1 ^ other; end\n" \
           "  wire tap = s1; assign y2 = tap;"
LOGIC_FIRST = "always @(posedge clk) begin s1 <= src & a; s2 <= s1; end"


def make_ws(tmp_path, rtl, clock=TWO_CLOCKS, cdc=None) -> Path:
    ws = tmp_path / "ws"
    (ws / "rtl").mkdir(parents=True)
    (ws / "spec").mkdir()
    (ws / "rtl" / "top.v").write_text(rtl, encoding="utf-8")
    spec = {"top": "top", "requirements": []}
    if clock:
        spec["clock"] = clock
    (ws / "spec" / "spec.yaml").write_text(yaml.safe_dump(spec),
                                           encoding="utf-8")
    if cdc is not None:
        (ws / "spec" / "cdc.yaml").write_text(yaml.safe_dump(cdc),
                                              encoding="utf-8")
    return ws


def run(ws) -> dict:
    payload, _ = check_cdc.run(["--workspace", str(ws)])
    return payload


def kinds(payload) -> list[str]:
    return sorted(v["kind"] for v in payload["violations"])


def rtl(sync: str) -> str:
    text = SOURCE.format(sync=sync)
    if "y2" in sync:
        text = text.replace("output wire y)", "output wire y, output wire y2)")
    return text


@pytest.mark.slow
def test_two_flop_synchroniser_is_clean(tmp_path):
    p = run(make_ws(tmp_path, rtl(TWO_FLOP)))
    assert p["status"] == "pass", p["violations"]
    assert p["domains"] == ["clk", "clk2"]
    assert [(c["status"], c["from"], c["to"], c["stages"])
            for c in p["crossings"]] == [("synchronized", "src", "s1", 2)]
    assert p["crosscheck"]["ran"] and p["crosscheck"]["match"]


@pytest.mark.slow
def test_missing_synchroniser_fails(tmp_path):
    p = run(make_ws(tmp_path, rtl(ONE_FLOP)))
    assert kinds(p) == ["cdc_unsynchronized"]
    assert p["violations"][0]["crossing"]["from"] == "src"


@pytest.mark.slow
def test_a_stage_deleted_from_the_synchroniser_fails(tmp_path):
    # s1 is gone: s2 samples src directly and drives the port
    text = rtl("always @(posedge clk) s2 <= src;").replace(
        "reg src, s1, s2, other;", "reg src, s2, other;")
    assert kinds(run(make_ws(tmp_path, text))) == ["cdc_unsynchronized"]


@pytest.mark.slow
def test_logic_ahead_of_the_synchroniser_fails(tmp_path):
    assert kinds(run(make_ws(tmp_path, rtl(LOGIC_FIRST)))) == [
        "cdc_comb_before_sync"]


@pytest.mark.slow
def test_waived_crossing_passes_and_is_listed(tmp_path):
    reason = "sampled once after reset, quasi-static"
    ws = make_ws(tmp_path, rtl(ONE_FLOP), cdc={"waivers": [
        {"from": "src", "to": "s1", "reason": reason}]})
    p = run(ws)
    assert p["status"] == "pass", p["violations"]
    assert [(w["kind"], w["reason"]) for w in p["waived"]] == [
        ("cdc_unsynchronized", reason)]


@pytest.mark.slow
def test_stale_waiver_is_a_finding(tmp_path):
    ws = make_ws(tmp_path, rtl(TWO_FLOP), cdc={"waivers": [
        {"from": "gone", "to": "s1", "reason": "was true once"}]})
    assert kinds(run(ws)) == ["cdc_waiver_unused"]


@pytest.mark.parametrize("cdc", [
    {"waivers": [{"from": "src", "to": "s1"}]},
    {"waivers": [{"from": "src", "to": "s1", "reason": "  "}]},
    {"waivers": [{"from": "*", "to": "s1", "reason": "all of it"}]},
    {"waivers": [{"clock": "*", "reason": "all of it"}]},
    {"waivers": [{"from": "src", "reason": "half a crossing"}]},
    {"allow": []},
])
def test_bad_cdc_yaml_is_refused(tmp_path, cdc):
    ws = make_ws(tmp_path, "module top; endmodule\n", cdc=cdc)
    with pytest.raises(checklib.CheckError):
        check_cdc.load_cdc_yaml(ws)


GATED = """\
module top (input wire clk, input wire a, input wire b, output wire y);
  reg en, q;
  wire gclk = {gate};
  always @({en_edge} clk) en <= {en_d};
  always @(posedge gclk) q <= a;
  assign y = q;
endmodule
"""
ONE_CLOCK = {"period_ns": 20, "domains": ["clk"]}


@pytest.mark.slow
@pytest.mark.parametrize("gate,en_edge,en_d", [
    ("clk & en", "negedge", "a"),
    ("clk | en", "posedge", "a"),
])
def test_opposite_edge_enable_is_a_safe_gate(tmp_path, gate, en_edge, en_d):
    p = run(make_ws(tmp_path, GATED.format(gate=gate, en_edge=en_edge,
                                           en_d=en_d), ONE_CLOCK))
    assert p["status"] == "pass", p["violations"]
    assert [(g["net"], g["safe"]) for g in p["clock_gates"]] == [("gclk", True)]
    assert p["crosscheck"]["match"]


@pytest.mark.slow
@pytest.mark.parametrize("gate,en_edge", [
    ("clk & en", "posedge"),         # changes while clk is high
    ("clk | en", "negedge"),         # changes while clk is low
    ("clk & (en | b)", "negedge"),   # logic on the enable
    ("clk & b", "negedge"),          # enable straight from a port
    ("clk ^ en", "negedge"),         # not an AND or OR
])
def test_glitchy_clock_gate_fails(tmp_path, gate, en_edge):
    p = run(make_ws(tmp_path, GATED.format(gate=gate, en_edge=en_edge,
                                           en_d="a"), ONE_CLOCK))
    assert kinds(p) == ["cdc_glitchy_clock_gate"]


MUX = """\
module top (input wire clk, input wire clk2, input wire s, output wire y);
  reg q;
  wire m = s ? clk : clk2;
  always @(posedge m) q <= ~q;
  assign y = q;
endmodule
"""


@pytest.mark.slow
def test_clock_mux_fails_and_waives(tmp_path):
    p = run(make_ws(tmp_path, MUX))
    assert kinds(p) == ["cdc_glitchy_clock_mux"]
    assert p["domains"] == ["clk", "clk2"]
    shutil.rmtree(tmp_path / "ws")
    p = run(make_ws(tmp_path, MUX, cdc={"waivers": [
        {"clock": "m", "reason": "select moves only in test mode"}]}))
    assert p["status"] == "pass", p["violations"]
    assert p["waived"][0]["kind"] == "cdc_glitchy_clock_mux"


@pytest.mark.slow
def test_async_port_declared_in_cdc_yaml_needs_a_synchroniser(tmp_path):
    text = """\
module top (input wire clk, input wire a, output reg y);
  always @(posedge clk) y <= a;
endmodule
"""
    p = run(make_ws(tmp_path, text, ONE_CLOCK))
    assert p["status"] == "pass", p["violations"]
    shutil.rmtree(tmp_path / "ws")
    p = run(make_ws(tmp_path, text, ONE_CLOCK, cdc={"ports": {"a": "async"}}))
    assert kinds(p) == ["cdc_unsynchronized"]


@pytest.mark.slow
def test_undeclared_clock_is_a_finding(tmp_path):
    p = run(make_ws(tmp_path, rtl(TWO_FLOP),
                    {"period_ns": 20, "domains": ["clk"], "primary": "clk"}))
    assert "cdc_clock_undeclared" in kinds(p)


@pytest.mark.slow
@pytest.mark.parametrize("body", [
    "wire c; reg q; always @(posedge c) q <= a; assign y = q;",
    "reg q; always @(posedge q) q <= ~q; assign y = q;",
])
def test_a_clock_with_no_source_is_a_finding(tmp_path, body):
    text = f"module top (input wire clk, input wire a, output wire y);\n" \
           f"  {body}\nendmodule\n"
    assert "cdc_clock_unknown" in kinds(run(make_ws(tmp_path, text, ONE_CLOCK)))


@pytest.mark.slow
def test_no_clock_info_is_not_checked_not_clean(tmp_path):
    ws = make_ws(tmp_path, rtl(ONE_FLOP), clock=None)
    p = run(ws)
    assert p["applicable"] is False and p["checked"] is False
    assert "NOT CHECKED" in p["reason"]
    assert "crossings" not in p


@pytest.mark.slow
def test_no_clock_info_reaches_gate_as_not_applicable(tmp_path):
    import gate
    ws = make_ws(tmp_path, rtl(ONE_FLOP), clock=None)
    report = tmp_path / "gate.json"
    rc = gate.main(["--gate", "cdc", "--skill", "vde", "--workspace", str(ws),
                    "--no-record", "--out", str(report)])
    assert rc == 0
    assert json.loads(report.read_text())["status"] == "not_applicable"


@pytest.mark.slow
def test_combinational_design_has_nothing_to_check(tmp_path):
    p = run(make_ws(tmp_path, "module top (input wire a, output wire y);\n"
                    "  assign y = ~a;\nendmodule\n", ONE_CLOCK))
    assert p["applicable"] is False
    assert "no sequential cell" in p["reason"]


def test_release_owes_cdc_unless_the_netlist_has_no_flop(tmp_path):
    spec = {"top": "top"}
    ws = tmp_path
    assert check_cdc.not_applicable_at_release(ws, spec) is None  # no netlist
    (ws / "synth").mkdir()
    path = ws / "synth" / "top.json"

    def netlist(cell):
        path.write_text(json.dumps({"modules": {"top": {"cells": {
            "c": {"type": cell}}}}}))
    netlist("gf180mcu_fd_sc_mcu7t5v0__dffrnq_1")
    assert check_cdc.not_applicable_at_release(ws, spec) is None
    netlist("gf180mcu_fd_sc_mcu7t5v0__latq_1")
    assert check_cdc.not_applicable_at_release(ws, spec) is None
    netlist("gf180mcu_fd_sc_mcu7t5v0__inv_1")
    assert "no sequential" in check_cdc.not_applicable_at_release(ws, spec)
    path.write_text(json.dumps({"modules": {"top": {"cells": {}}}}))
    assert check_cdc.not_applicable_at_release(ws, spec) is None


@pytest.mark.slow
def test_pll_digital_known_crossings(tmp_path):
    """The known answer (issue #88): pfd_dn single flop, n_sel async into the
    divider, gated clk_fb, the obs clock mux, plus the PFD's mutual clear
    that constraints.sdc names as crossing on purpose."""
    block = REPO / "blocks" / "pll" / "digital"
    ws = tmp_path / "pll"
    (ws / "spec").mkdir(parents=True)
    shutil.copytree(block / "rtl", ws / "rtl")
    shutil.copy(block / "spec" / "spec.yaml", ws / "spec" / "spec.yaml")
    p = run(ws)
    assert p["domains"] == ["clk", "vco_out"]
    assert p["crosscheck"]["ran"] and p["crosscheck"]["match"], p["crosscheck"]
    assert {c["net"]: c["kind"] for c in p["clocks"] if c["kind"] != "port"} \
        == {"u_div.clk_fb": "gated", "u_div.clk_pre": "divided",
            "u_pre.pre_q0": "divided", "u_pre.pre_q1": "divided"}
    assert [(c["status"], c["from"], c["to"]) for c in p["crossings"]] == [
        ("unsynchronized", "u_div.n_sel", "u_div.div_cnt"),
        ("unsynchronized", "u_div.n_sel", "u_div.fb_en"),
        ("unsynchronized", "u_pfd.pfd_dn", "u_lock.wide_q"),
        ("async_reset", "u_pfd.pfd_dn", "u_pfd.pfd_ref_q"),
        ("async_reset", "u_pfd.pfd_ref_q", "u_pfd.pfd_dn"),
    ]
    gates = {g["net"]: g for g in p["clock_gates"]}
    assert set(gates) == {"obs_int", "u_div.clk_fb"}
    assert gates["u_div.clk_fb"]["safe"] is True
    assert gates["obs_int"]["kind"] == "mux" and not gates["obs_int"]["safe"]
    assert kinds(p) == sorted(
        ["cdc_unsynchronized"] * 3 + ["cdc_async_reset_crossing"] * 2
        + ["cdc_glitchy_clock_mux"])

    # with every finding waived, the gate passes and lists them all
    (ws / "spec" / "cdc.yaml").write_text(yaml.safe_dump({"waivers": [
        {"from": "*n_sel", "to": "u_div.*", "reason": "quasi-static"},
        {"from": "pfd_dn_q", "to": "u_lock.wide_q", "reason": "single sample"},
        {"from": "u_pfd.pfd_*", "to": "u_pfd.pfd_*", "reason": "PFD clear"},
        {"clock": "obs_int", "reason": "observation only"},
    ]}), encoding="utf-8")
    p = run(ws)
    assert p["status"] == "pass", p["violations"]
    assert len(p["waived"]) == 6
