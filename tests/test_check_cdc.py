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


def make_ws(tmp_path, rtl, clock=TWO_CLOCKS, cdc=None, tt_pins=None,
            extra=None) -> Path:
    ws = tmp_path / "ws"
    (ws / "rtl").mkdir(parents=True)
    (ws / "spec").mkdir()
    (ws / "rtl" / "top.v").write_text(rtl, encoding="utf-8")
    spec = {"top": "top", "requirements": []}
    if clock:
        spec["clock"] = clock
    if tt_pins:
        spec["tt_pins"] = tt_pins
    spec.update(extra or {})
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


SAMPLE_A = """\
module top (input wire clk, input wire a, output reg y);
  always @(posedge clk) y <= a;
endmodule
"""
ONE_CLOCK = {"period_ns": 20, "domains": ["clk"]}


@pytest.mark.slow
def test_an_undeclared_port_is_listed_as_assumed(tmp_path):
    """`a` takes the one domain because nothing declared it (#169)."""
    p = run(make_ws(tmp_path, SAMPLE_A, clock=ONE_CLOCK))
    assert p["status"] == "pass", p["violations"]
    assert p["assumed_ports"] == ["a"]


@pytest.mark.slow
def test_a_declared_port_is_not_assumed(tmp_path):
    p = run(make_ws(tmp_path, SAMPLE_A, clock=ONE_CLOCK,
                    cdc={"ports": {"a": "clk"}}))
    assert p["status"] == "pass", p["violations"]
    assert p["assumed_ports"] == []


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
        + ["cdc_glitchy_clock_mux"] + ["rdc_unsynced_release"] * 2
        + ["rdc_comb_reset"] * 2)
    # #90: the raw rst_n reaches both root domains, the issue's four were
    # clock nets (the divided clocks are in vco_out's domain, and STA times
    # them), and the PFD's clear is logic in each domain.
    resets = {(v["reset"]["net"], tuple(v["reset"]["domain"])): v["reset"]
              for v in p["violations"] if "reset" in v}
    assert set(resets) == {("rst_n", ("clk",)), ("rst_n", ("vco_out",)),
                           ("u_pfd.pfd_clr", ("clk",)),
                           ("u_pfd.pfd_clr", ("vco_out",))}
    assert resets["rst_n", ("vco_out",)]["clock_nets"] == [
        "u_div.clk_fb", "u_div.clk_pre", "u_pre.pre_q0", "u_pre.pre_q1",
        "u_pre.vco_out"]
    assert p["reset_syncs"] == []

    # with every finding waived, the gate passes and lists them all
    (ws / "spec" / "cdc.yaml").write_text(yaml.safe_dump({"waivers": [
        {"from": "*n_sel", "to": "u_div.*", "reason": "quasi-static"},
        {"from": "pfd_dn_q", "to": "u_lock.wide_q", "reason": "single sample"},
        {"from": "u_pfd.pfd_*", "to": "u_pfd.pfd_*", "reason": "PFD clear"},
        {"clock": "obs_int", "reason": "observation only"},
        {"reset": "rst_n", "reason": "held until the clocks run"},
        {"reset": "u_pfd.pfd_clr", "reason": "PFD clear, the function"},
    ]}), encoding="utf-8")
    p = run(ws)
    assert p["status"] == "pass", p["violations"]
    assert len(p["waived"]) == 10


# ---- reset release (#90) -------------------------------------------------

ONE_CLOCK = {"period_ns": 20, "domains": ["clk"]}

# counter8 with an async reset; {sync} defines rst_i from the raw rst_n.
COUNTER = """\
module top (input wire clk, input wire clk2, input wire rst_n, input wire a,
            output reg [7:0] count, output reg y);
  wire rst_i;
  reg s1, s2, s3, t1, t2, plain;
  {sync}
  always @(posedge clk or negedge rst_i)
    if (!rst_i) count <= 8'd0; else count <= count + 8'd1;
endmodule
"""
SYNC2 = """always @(posedge clk or negedge rst_n)
    if (!rst_n) {s2, s1} <= 2'b00; else {s2, s1} <= {s1, 1'b1};
  assign rst_i = s2;"""


def counter(sync: str) -> str:
    return COUNTER.format(sync=sync)


def rdc_kinds(p) -> list[str]:
    return [k for k in kinds(p) if k.startswith("rdc_")]


@pytest.mark.slow
def test_counter_with_two_flop_reset_sync_passes(tmp_path):
    p = run(make_ws(tmp_path, counter(SYNC2), ONE_CLOCK))
    assert p["status"] == "pass", p["violations"]
    assert [(s["net"], s["stages"], s["reset"], s["domain"])
            for s in p["reset_syncs"]] == [("rst_i", 2, "rst_n", ["clk"])]
    assert {(r["net"], r["status"], r["flops"]) for r in p["reset_trees"]} == {
        ("rst_n", "sync_stage", 2), ("rst_i", "synchronized", 8)}


@pytest.mark.slow
def test_three_stage_reset_sync_passes(tmp_path):
    sync = """always @(posedge clk or negedge rst_n)
    if (!rst_n) {s3, s2, s1} <= 3'b0; else {s3, s2, s1} <= {s2, s1, 1'b1};
  assign rst_i = s3;"""
    p = run(make_ws(tmp_path, counter(sync), ONE_CLOCK))
    assert p["status"] == "pass", p["violations"]
    assert [s["stages"] for s in p["reset_syncs"]] == [3]


@pytest.mark.slow
def test_active_high_reset_sync_passes(tmp_path):
    rtl = """\
module top (input wire clk, input wire rst, output reg [1:0] c);
  reg s1, s2;
  always @(posedge clk or posedge rst)
    if (rst) {s2, s1} <= 2'b11; else {s2, s1} <= {s1, 1'b0};
  always @(posedge clk or posedge s2)
    if (s2) c <= 2'd0; else c <= c + 2'd1;
endmodule
"""
    p = run(make_ws(tmp_path, rtl, ONE_CLOCK))
    assert p["status"] == "pass", p["violations"]


@pytest.mark.slow
def test_sync_read_as_a_synchronous_reset_is_still_a_sync(tmp_path):
    """A stage's raw reset is the point of it, whoever reads the output."""
    rtl = """\
module top (input wire clk, input wire rst_n, output reg c);
  reg s1, s2;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) {s2, s1} <= 2'b00; else {s2, s1} <= {s1, 1'b1};
  always @(posedge clk) if (!s2) c <= 1'b0; else c <= ~c;
endmodule
"""
    p = run(make_ws(tmp_path, rtl, ONE_CLOCK))
    assert p["status"] == "pass", p["violations"]


@pytest.mark.slow
@pytest.mark.parametrize("sync", [
    # the second flop removed
    """always @(posedge clk or negedge rst_n)
    if (!rst_n) s1 <= 1'b0; else s1 <= 1'b1;
  assign rst_i = s1;""",
    # D not tied
    """always @(posedge clk or negedge rst_n)
    if (!rst_n) {s2, s1} <= 2'b00; else {s2, s1} <= {s1, a};
  assign rst_i = s2;""",
    # D tied to the reset value: never releases
    """always @(posedge clk or negedge rst_n)
    if (!rst_n) {s2, s1} <= 2'b00; else {s2, s1} <= {s1, 1'b0};
  assign rst_i = s2;""",
    # the first stage has another load
    SYNC2 + "\n  always @(posedge clk) y <= s1;",
    # logic between the stages
    """always @(posedge clk or negedge rst_n)
    if (!rst_n) {s2, s1} <= 2'b00; else {s2, s1} <= {s1 & a, 1'b1};
  assign rst_i = s2;""",
    # stages on opposite edges
    """always @(posedge clk or negedge rst_n)
    if (!rst_n) s1 <= 1'b0; else s1 <= 1'b1;
  always @(negedge clk or negedge rst_n)
    if (!rst_n) s2 <= 1'b0; else s2 <= s1;
  assign rst_i = s2;""",
], ids=["no_second_flop", "d_not_tied", "d_is_reset_value", "first_stage_tapped",
        "logic_between", "opposite_edges"])
def test_not_a_reset_sync_fails(tmp_path, sync):
    p = run(make_ws(tmp_path, counter(sync), ONE_CLOCK))
    assert p["status"] == "violations"
    assert "rdc_unsynced_release" in kinds(p), p["violations"]
    assert p["reset_syncs"] == []
    assert "rdc_comb_reset" not in kinds(p)


@pytest.mark.slow
def test_raw_reset_fails_waives_per_domain_and_stale_waiver_found(tmp_path):
    ws = make_ws(tmp_path, counter("assign rst_i = rst_n;"), ONE_CLOCK)
    p = run(ws)
    assert rdc_kinds(p) == ["rdc_unsynced_release"]
    v = next(v for v in p["violations"] if "reset" in v)
    assert (v["reset"]["net"], v["reset"]["domain"], v["reset"]["flops"]) == (
        "rst_n", ["clk"], 8)

    cdc = ws / "spec" / "cdc.yaml"
    cdc.write_text(yaml.safe_dump({"waivers": [
        {"reset": "rst_n", "domain": "clk", "reason": "two cycles of margin"},
    ]}), encoding="utf-8")
    p = run(ws)
    assert p["status"] == "pass", p["violations"]
    assert [w["kind"] for w in p["waived"]] == ["rdc_unsynced_release"]

    cdc.write_text(yaml.safe_dump({"waivers": [
        {"reset": "rst_n", "domain": "clk2", "reason": "another domain"},
    ]}), encoding="utf-8")
    p = run(ws)
    assert kinds(p) == ["cdc_waiver_unused", "rdc_unsynced_release"]


@pytest.mark.slow
def test_port_declared_synchronous_only_counts_in_its_domain(tmp_path):
    ws = make_ws(tmp_path, counter("assign rst_i = rst_n;"), ONE_CLOCK,
                 cdc={"ports": {"rst_n": "clk"}})
    p = run(ws)
    assert p["status"] == "pass", p["violations"]
    assert p["reset_trees"][0]["status"] == "declared_synchronous"

    rtl = counter("assign rst_i = rst_n;\n"
                  "  always @(posedge clk2 or negedge rst_n)\n"
                  "    if (!rst_n) plain <= 1'b0; else plain <= a;\n"
                  "  always @(posedge clk2) y <= plain;")
    (tmp_path / "two").mkdir()
    ws = make_ws(tmp_path / "two", rtl, TWO_CLOCKS,
                 cdc={"ports": {"rst_n": "clk", "a": "clk2"}})
    p = run(ws)
    assert [v["reset"]["domain"] for v in p["violations"]
            if v["kind"] == "rdc_unsynced_release"] == [["clk2"]]


@pytest.mark.slow
def test_reset_anded_with_data_is_a_comb_reset(tmp_path):
    p = run(make_ws(tmp_path, counter(SYNC2.replace(
        "assign rst_i = s2;", "assign rst_i = s2 & a;")), ONE_CLOCK))
    # `a` is a port on the reset net, so it is a raw release as well
    assert rdc_kinds(p) == ["rdc_comb_reset", "rdc_unsynced_release"]
    assert {v["reset"]["net"] for v in p["violations"]} == {"rst_i", "a"}
    # the same logic with a flop in place of the port: only the logic
    (tmp_path / "ff").mkdir()
    sync = SYNC2.replace("assign rst_i = s2;", """\
always @(posedge clk or negedge s2)
    if (!s2) plain <= 1'b0; else plain <= a;
  assign rst_i = s2 & plain;""")
    p = run(make_ws(tmp_path / "ff", counter(sync), ONE_CLOCK))
    assert rdc_kinds(p) == ["rdc_comb_reset"]
    # raw, the release is judged as well: waiving the logic hides nothing
    (tmp_path / "raw").mkdir()
    p = run(make_ws(tmp_path / "raw", counter("assign rst_i = rst_n & a;"),
                    ONE_CLOCK))
    assert rdc_kinds(p) == ["rdc_comb_reset", "rdc_unsynced_release",
                            "rdc_unsynced_release"]


@pytest.mark.slow
def test_flop_with_no_reset_on_an_async_pin(tmp_path):
    sync = "always @(posedge clk) plain <= a;\n  assign rst_i = plain;"
    p = run(make_ws(tmp_path, counter(sync), ONE_CLOCK))
    assert rdc_kinds(p) == ["rdc_nonreset_drives_reset"]
    # a sync whose last stage has no reset is this, and not a sync
    sync = """always @(posedge clk or negedge rst_n)
    if (!rst_n) s1 <= 1'b0; else s1 <= 1'b1;
  always @(posedge clk) s2 <= s1;
  assign rst_i = s2;"""
    (tmp_path / "b").mkdir()
    p = run(make_ws(tmp_path / "b", counter(sync), ONE_CLOCK))
    assert rdc_kinds(p) == ["rdc_nonreset_drives_reset",
                            "rdc_unsynced_release"]


@pytest.mark.slow
def test_soft_reset_register_in_its_own_domain_is_clean(tmp_path):
    """A flop with a reset, timed by STA, itself reset from the sync."""
    sync = SYNC2.replace("assign rst_i = s2;", """\
always @(posedge clk or negedge s2)
    if (!s2) plain <= 1'b0; else plain <= 1'b1;
  assign rst_i = plain;""")
    p = run(make_ws(tmp_path, counter(sync), ONE_CLOCK))
    assert p["status"] == "pass", p["violations"]


TWO_DOMAINS = """\
module top (input wire clk, input wire clk2, input wire rst_n,
            output reg a, output reg b);
  reg s1, s2, t1, t2;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) {s2, s1} <= 2'b00; else {s2, s1} <= {s1, 1'b1};
  {second}
  always @(posedge clk or negedge s2)
    if (!s2) a <= 1'b0; else a <= ~a;
  always @(posedge clk2 or negedge {reset_b})
    if (!{reset_b}) b <= 1'b0; else b <= ~b;
endmodule
"""


@pytest.mark.slow
@pytest.mark.parametrize("second,reset_b,want", [
    # a synchroniser in each domain
    ("always @(posedge clk2 or negedge rst_n) if (!rst_n) {t2, t1} <= 2'b00;"
     " else {t2, t1} <= {t1, 1'b1};", "t2", []),
    # chained: clk2's sync takes its assert from clk's, its release in clk2
    ("always @(posedge clk2 or negedge s2) if (!s2) {t2, t1} <= 2'b00;"
     " else {t2, t1} <= {t1, 1'b1};", "t2", []),
    # one domain raw
    ("", "rst_n", ["rdc_unsynced_release"]),
    # clk's synchroniser used in clk2: the crossing is the one finding
    ("", "s2", ["cdc_async_reset_crossing"]),
], ids=["each_domain", "chained", "one_raw", "wrong_domain"])
def test_release_is_per_domain(tmp_path, second, reset_b, want):
    rtl = TWO_DOMAINS.replace("{second}", second).replace("{reset_b}", reset_b)
    p = run(make_ws(tmp_path, rtl, TWO_CLOCKS))
    assert kinds(p) == want, p["violations"]


@pytest.mark.slow
def test_synchronous_reset_has_no_tree(tmp_path):
    rtl = """\
module top (input wire clk, input wire rst, output reg c);
  always @(posedge clk) if (rst) c <= 1'b0; else c <= ~c;
endmodule
"""
    p = run(make_ws(tmp_path, rtl, ONE_CLOCK))
    assert p["status"] == "pass", p["violations"]
    assert p["reset_trees"] == []


# ---- a TT tile's pad reset defaults to async (#179) ---------------------

# r2r_dac's shape: the pad reset registered once, then a synchronous reset.
PAD_RESET = """\
module top (input wire clk, input wire rst_n, input wire [3:0] ui_in,
            output reg [3:0] q);
  reg rst_q, rst_q2;
  {sample}
  always @(posedge clk) if (!{rst}) q <= 4'd0; else q <= q + ui_in;
endmodule
"""
ONE_SAMPLE = "always @(posedge clk) rst_q <= rst_n;"
TWO_SAMPLE = "always @(posedge clk) begin rst_q <= rst_n; rst_q2 <= rst_q; end"
TT_PINS = {"clk": "clk", "rst_n": "rst_n", "ui_in": "ui_in[3:0]",
           "q": "uo_out[3:0]"}


def pad_reset(sample, rst):
    return PAD_RESET.format(sample=sample, rst=rst)


@pytest.mark.slow
def test_one_flop_sample_of_the_tt_pad_reset_fails(tmp_path):
    p = run(make_ws(tmp_path, pad_reset(ONE_SAMPLE, "rst_q"), ONE_CLOCK,
                    tt_pins=TT_PINS))
    assert kinds(p) == ["cdc_unsynchronized"], p["violations"]
    [c] = [c for c in p["crossings"] if c["status"] != "synchronized"]
    assert (c["from"], c["to"]) == ("rst_n", "rst_q"), c
    assert {"port": "rst_n", "domain": "async",
            "source": "tt_pins: rst_n -> rst_n (the tile's pad reset)"} \
        in p["port_domains"]


@pytest.mark.slow
def test_block_without_tt_pins_keeps_the_primary_default(tmp_path):
    p = run(make_ws(tmp_path, pad_reset(ONE_SAMPLE, "rst_q"), ONE_CLOCK))
    assert p["status"] == "pass", p["violations"]
    assert p["port_domains"] == [] and p["assumptions"] == []


@pytest.mark.slow
def test_two_flop_sampler_of_the_tt_pad_reset_passes(tmp_path):
    p = run(make_ws(tmp_path, pad_reset(TWO_SAMPLE, "rst_q2"), ONE_CLOCK,
                    tt_pins=TT_PINS))
    assert p["status"] == "pass", p["violations"]


@pytest.mark.slow
def test_explicit_port_claim_overrides_the_pad_reset_default(tmp_path):
    p = run(make_ws(tmp_path, pad_reset(ONE_SAMPLE, "rst_q"), ONE_CLOCK,
                    cdc={"ports": {"rst_n": "clk"}}, tt_pins=TT_PINS))
    assert p["status"] == "pass", p["violations"]
    assert p["port_domains"] == [
        {"port": "rst_n", "domain": "clk", "source": "spec/cdc.yaml"}]


@pytest.mark.slow
def test_inverted_pad_reset_is_async_too(tmp_path):
    text = """\
module top (input wire clk, input wire rst, output reg q);
  reg rst_q;
  always @(posedge clk) rst_q <= rst;
  always @(posedge clk) if (rst_q) q <= 1'b0; else q <= ~q;
endmodule
"""
    p = run(make_ws(tmp_path, text, ONE_CLOCK,
                    tt_pins={"clk": "clk", "rst": "~rst_n", "q": "uo_out[0]"}))
    assert kinds(p) == ["cdc_unsynchronized"], p["violations"]


@pytest.mark.slow
def test_ui_in_keeps_the_primary_default_and_is_an_assumption(tmp_path):
    p = run(make_ws(tmp_path, pad_reset(TWO_SAMPLE, "rst_q2"),
                    {"period_ns": 20, "domains": ["clk"], "primary": "clk"},
                    tt_pins=TT_PINS))
    assert p["status"] == "pass", p["violations"]
    [a] = p["assumptions"]
    assert a.startswith("ui_in (tt_pins ui_in[3:0]) is taken as synchronous "
                        "to clock.primary (clk)"), a


@pytest.mark.slow
def test_pad_reset_through_a_reset_synchroniser_still_passes(tmp_path):
    p = run(make_ws(tmp_path, counter(SYNC2), ONE_CLOCK,
                    tt_pins={"clk": "clk", "rst_n": "rst_n"}))
    assert p["status"] == "pass", p["violations"]


# ---- scope: through the generated wrapper's pad reset synchroniser -------

# The counter's shape: every flop cleared straight from the block's rst_n,
# no synchroniser of its own. Alone, rst_n async, that is an unsynced
# release; as it ships, behind the wrapper's rst_n_sync_q1/q2, it is not.
RAW_RESET = """\
module top (input wire clk, input wire rst_n, input wire [3:0] ui_in,
            output reg [3:0] q);
  always @(posedge clk or negedge rst_n)
    if (!rst_n) q <= 4'd0; else q <= q + ui_in;
endmodule
"""
TILE_PORTS = {"ports": {"clk": {"dir": "input"}, "rst_n": {"dir": "input"},
                        "ui_in": {"dir": "input", "width": 4},
                        "q": {"dir": "output", "width": 4}}}
OPT_OUT = {"pad_reset_sync": {"mode": "off", "reason": "a test, see #0"}}


def tile(tmp_path, rtl, extra=(), cdc=None):
    return make_ws(tmp_path, rtl, ONE_CLOCK, tt_pins=TT_PINS, cdc=cdc,
                   extra={**TILE_PORTS, **dict(extra)})


@pytest.mark.slow
def test_tile_through_the_wrapper_sees_its_synchroniser(tmp_path):
    p = run(tile(tmp_path, RAW_RESET))
    assert p["status"] == "pass", p["violations"]
    assert p["scope"]["scope"] == "wrapper+block", p["scope"]
    assert p["top"] == "tt_um_top" and p["block"] == "top"
    [sync] = [r for r in p["reset_syncs"] if r["reset"] == "rst_n"]
    assert sync["stage_nets"][0] == "rst_n_sync_q1", sync
    assert p["port_domains"][0]["port"] == "rst_n"
    assert p["port_domains"][0]["domain"] == "async"
    [a] = p["assumptions"]
    assert a.startswith("ui_in (tt_pins ui_in[3:0])"), a


@pytest.mark.slow
def test_one_sample_behind_the_wrapper_passes(tmp_path):
    # r2r_dac's shape: synchronous inside, the wrapper closed the crossing
    p = run(tile(tmp_path, pad_reset(ONE_SAMPLE, "rst_q")))
    assert p["status"] == "pass", p["violations"]
    assert p["scope"]["scope"] == "wrapper+block"


@pytest.mark.slow
def test_opted_out_tile_rst_n_is_still_checked_as_async(tmp_path):
    p = run(tile(tmp_path, RAW_RESET, OPT_OUT))
    assert p["scope"]["scope"] == "block", p["scope"]
    assert "pad_reset_sync off: a test, see #0" in p["scope"]["why"]
    assert "rdc_unsynced_release" in kinds(p), p["violations"]
    p = run(tile(tmp_path / "b", pad_reset(ONE_SAMPLE, "rst_q"), OPT_OUT))
    assert kinds(p) == ["cdc_unsynchronized"], p["violations"]


@pytest.mark.slow
def test_waivers_keep_the_tile_in_block_scope(tmp_path):
    p = run(tile(tmp_path, RAW_RESET, cdc={"waivers": [
        {"reset": "rst_n", "reason": "a test"}]}))
    assert p["scope"]["scope"] == "block", p["scope"]
    assert "has waivers" in p["scope"]["why"]
    assert p["status"] == "pass", p["violations"]
    assert [w["kind"] for w in p["waived"]] == ["rdc_unsynced_release"]


def test_scope_without_a_synchroniser_is_the_block(tmp_path):
    ws = make_ws(tmp_path, RAW_RESET, ONE_CLOCK, tt_pins=TT_PINS)
    spec = check_cdc.speclib.load_spec(ws / "spec" / "spec.yaml")
    wrapped, why = check_cdc.analysis_scope(
        spec, check_cdc.load_cdc_yaml(ws), ["clk"])
    assert not wrapped and "the block alone" in why


def test_scope_with_a_reasonless_opt_out_refuses(tmp_path):
    spec = {"top": "top", "tt_pins": TT_PINS, **TILE_PORTS,
            "pad_reset_sync": {"mode": False}}
    with pytest.raises(check_cdc.CheckError, match="pad_reset_sync_no_reason"):
        check_cdc.analysis_scope(spec, {"ports": {}, "waivers": []}, ["clk"])


# --- #169: an msde comparator output the split says is async --------------

CMP_IFACE = """\
version: 1
signals:
  - {name: cmp_out, direction: a2d, level: cmos_3v3, domain: clk_free,
     width: 1, load: 2pF}
"""
CMP_SIDE = """\
top: {top}
interface:
  - {{name: cmp_out, direction: a2d, level: cmos_3v3, domain: clk_free,
     width: 1, load: 2pF}}
"""
CMP_RTL = """\
module cmp_digital (input wire clk, input wire rst_n, input wire cmp_out,
                    output reg [3:0] count);
  reg s1, s2;
  always @(posedge clk or negedge rst_n)
    if (!rst_n) begin s1 <= 1'b0; s2 <= 1'b0; end
    else begin s1 <= cmp_out; s2 <= s1; end
  always @(posedge clk or negedge rst_n)
    if (!rst_n) count <= 4'd0;
    else if ({tap}) count <= count + 4'd1;
endmodule
"""


def _comparator_block(tmp_path, tap: str) -> Path:
    """An msde block whose digital side counts an async comparator output;
    `tap` is the flop the counter reads (s1: one flop, s2: two)."""
    import check_split  # noqa: E402
    ws = tmp_path / "blk"
    ws.mkdir()
    (ws / check_split.INTERFACE_REL).write_text(CMP_IFACE, encoding="utf-8")
    (ws / "digital_spec.yaml").write_text(
        CMP_SIDE.format(top="cmp_digital"), encoding="utf-8")
    (ws / "analog_spec.yaml").write_text(
        CMP_SIDE.format(top="cmp_analog"), encoding="utf-8")
    dig = make_ws(tmp_path, CMP_RTL.format(tap=tap),
                  clock={"period_ns": 20, "domains": ["clk"]},
                  tt_pins={"clk": "clk", "rst_n": "rst_n",
                           "cmp_out": "ui_in[0]", "count": "uo_out[3:0]"},
                  extra={"top": "cmp_digital", "ports": {
                      "clk": {"dir": "input", "width": 1},
                      "rst_n": {"dir": "input", "width": 1},
                      "cmp_out": {"dir": "input", "width": 1},
                      "count": {"dir": "output", "width": 4}}})
    shutil.move(str(dig), str(ws / "digital"))
    return ws


@pytest.mark.slow
@pytest.mark.parametrize("tap,want", [("s1", ["cdc_unsynchronized"]),
                                      ("s2", [])])
def test_issue169_async_comparator_needs_two_flops(tmp_path, tap, want):
    """The issue's planted fault: one flop on an async comparator output,
    no spec/cdc.yaml. Before #169 the cdc gate took cmp_out as clk and
    passed. Now the split refuses the missing declaration, split_cdc writes
    it, and the cdc gate then fails the one-flop sample (known answer: the
    two-flop chain is clean)."""
    import check_split  # noqa: E402
    import split_cdc  # noqa: E402
    ws = _comparator_block(tmp_path, tap)

    before = run(ws / "digital")
    assert before["scope"]["scope"] == "wrapper+block"
    assert before["status"] == "pass", before["violations"]  # the bug
    assert before["assumed_ports"] == ["ui_in"]
    split, _ = check_split.run(["--workspace", str(ws)])
    assert sorted(v["kind"] for v in split["violations"]) == \
        ["cdc_async_undeclared"]

    wrote, _ = split_cdc.run(["--workspace", str(ws)])
    assert wrote["written"] == ["cmp_out"]
    split, _ = check_split.run(["--workspace", str(ws)])
    assert split["status"] == "pass", split["violations"]

    after = run(ws / "digital")
    assert after["assumed_ports"] == []
    assert kinds(after) == want, after["violations"]
    assert after["status"] == ("violations" if want else "pass")
