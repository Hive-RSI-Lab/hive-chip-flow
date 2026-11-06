"""engine/lib/leclib.py and engine/scripts/check_lec.py: the normal form by
hand, then real yosys, eqy and kepler-formal on a counter8 run built by
tests/lec_fixture.py, with planted faults that have known answers."""
from __future__ import annotations

import importlib.util
import json
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "scripts"))
sys.path.insert(0, str(REPO / "engine" / "lib"))
sys.path.insert(0, str(REPO / "tests"))

import check_glsim  # noqa: E402
import check_lec  # noqa: E402
import checklib  # noqa: E402
import lec_fixture as fx  # noqa: E402
import leclib  # noqa: E402
import speclib  # noqa: E402


# counter8's ports with a state machine yosys's fsm pass recognises and recodes.
FSM = """\
module counter8 (input wire clk, input wire rst, output reg [7:0] count);
  localparam A = 2'd0, B = 2'd1, C = 2'd2, D = 2'd3;
  reg [1:0] state;
  always @(posedge clk)
    if (rst) begin state <= A; count <= 8'd0; end
    else case (state)
      A: begin state <= B; count <= count + 8'd1; end
      B: begin state <= C; count <= count + 8'd2; end
      C: begin state <= D; count <= count + 8'd4; end
      D: begin state <= A; count <= count + 8'd8; end
    endcase
endmodule
"""


def _plant_module():
    path = REPO / "corpus" / "vde" / "counter8" / "faults" / "plant_lec.py"
    spec = importlib.util.spec_from_file_location("plant_lec", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run(ws: Path) -> dict:
    payload, _ = check_lec.run(["--workspace", str(ws)])
    return payload


def kinds(payload: dict) -> list[str]:
    return sorted({v["kind"] for v in payload["violations"]})


def points(payload: dict, link: str) -> dict:
    return {(v.get("key_point") or v.get("output")): v.get("pin_kind")
            for v in payload["violations"] if v.get("link") == link}


@pytest.fixture(scope="module")
def reference(tmp_path_factory) -> Path:
    return fx.build(tmp_path_factory.mktemp("lec") / "ws")


@pytest.fixture
def ws(reference, tmp_path) -> Path:
    copy = tmp_path / "ws"
    shutil.copytree(reference, copy, ignore=shutil.ignore_patterns("log"))
    return copy


# ---- no tools --------------------------------------------------------------

LIBERTY = """\
library (x) {
  cell (a__inv_1) { pin (I) { direction : input; }
    pin (ZN) { direction : output; function : "!I"; } }
  cell (a__dff_1) { ff (IQ, IQN) { clocked_on : "CLK"; next_state : "D"; }
    pin (Q) { direction : output; function : "IQ"; } }
  cell (a__icg_1) { pin (Q) { direction : output; } }
  cell (a__bufz_1) { pin (Z) { direction : output; function : "I";
    three_state : "!EN"; } }
  cell (a__fill_1) { area : 1; }
}
"""


def test_liberty_scan_finds_what_has_no_model():
    sequential, unmodelled = leclib.scan_liberty(LIBERTY)
    assert sequential == {"a__dff_1"}
    assert set(unmodelled) == {"a__icg_1", "a__bufz_1"}


def test_cells_used_reads_instances_not_module_headers():
    text = "module a__stub_1 (input I);\nendmodule\n" \
           "  (* keep *) a__icg_1 u0 (.Q(x));\n  a__dff_1 \\u.q (.D(d));\n"
    assert leclib.cells_used(text) == {"a__icg_1", "a__dff_1"}


@pytest.mark.parametrize("cell,key_by,want", [
    ("\\u.q_ff", "net", "u.q"),
    ("\\u.q[3]_ff.IQ1_ff", "net", "u.q[3]"),
    ("\\_143_.IQ1_ff", "instance", "_143_"),
    ("$auto$ff.cc:1$2", "net", "$auto$ff.cc:1$2"),
])
def test_key_name(cell, key_by, want):
    assert leclib.key_name(cell, key_by) == want


def test_alias_table_and_rekey_take_the_other_views_name():
    nets = {"modules": {"t": {"netnames": {
        "u.q": {"bits": [5], "hide_name": 0},
        "out": {"bits": [5, 6], "hide_name": 0},
        "$hid": {"bits": [5], "hide_name": 1}}}}}
    aliases = leclib.alias_table(json.dumps(nets), "t")
    assert aliases["u.q"] == {"u.q", "out[0]"}
    view = leclib.View("t", [], {"u.q": leclib.KeyPoint("u.q", "ff")}, [], [])
    leclib.rekey_by_alias(view, aliases, {"out[0]"})
    assert set(view.key_points) == {"out[0]"}


def test_q_net_is_the_designs_name_not_the_cell_models():
    kp = leclib.KeyPoint("_143_", "ff", {"Q": "\\_143_.IQ1"})
    view = leclib.View("t", [], {"_143_": kp}, [], [])
    assert kp.q_net() == "_143_.IQ1"
    leclib.name_q_nets(view, {"_143_.IQ1": {"_143_.IQ1", "_143_.Q", "u.recover"}})
    assert kp.q_net() == "u.recover"


def test_two_key_points_on_one_alias_refuse():
    view = leclib.View("t", [], {"a": leclib.KeyPoint("a", "ff"),
                                 "b": leclib.KeyPoint("b", "ff")}, [], [])
    with pytest.raises(leclib.LecError):
        leclib.rekey_by_alias(view, {"a": {"a", "x"}, "b": {"b", "x"}}, {"x"})


RTLIL = """\
module \\t
  attribute \\src "x"
  wire input 1 \\clk
  wire input 2 \\d
  wire output 3 \\y
  wire \\mid
  cell $_NOT_ $n
    connect \\A \\mid
    connect \\Y \\y
  end
  cell \\lec_ff \\m_ff
    connect \\C \\clk
    connect \\S 1'0
    connect \\R 1'0
    connect \\D \\d
    connect \\Q \\mid
  end
end
"""


def test_render_makes_flops_ports_and_hides_everything_else():
    view = leclib.parse_view(RTLIL, "t", "net")
    assert set(view.key_points) == {"m"} and view.outputs == ["\\y"]
    text = leclib.render(view, {"m"})
    decls = re.findall(r"wire (input|output) \d+ (\S+)", text)
    assert sorted(decls) == sorted([
        ("input", "\\clk"), ("input", "\\d"), ("input", "\\m.Q"),
        ("output", "\\m.C"), ("output", "\\m.S"), ("output", "\\m.R"),
        ("output", "\\m.D"), ("output", "\\y")])
    assert "\\mid" not in text, "an internal name would be a cut for eqy"
    assert "lec_ff" not in text and "attribute" not in text
    first_use = text.index("connect")
    assert text.rfind("  wire ") < first_use, "RTLIL declares before use"


def test_render_leaves_out_pins_of_a_key_point_the_other_view_lacks():
    view = leclib.parse_view(RTLIL, "t", "net")
    text = leclib.render(view, {"other"})
    assert "\\m.D" not in text and "input" in text and "\\m.Q" in text
    assert re.search(r"wire input \d+ \\other\.Q", text)


def test_partition_names_and_points():
    assert leclib.partition_name("u.q[3].D") == "u.q.3.D"
    assert leclib.describe_point("u.q.C")["pin_kind"] == "clock"
    assert leclib.describe_point("uo_out[1]") == {"output": "uo_out[1]"}


RECODE_LOG = """\
Recoding FSM `$fsm$\\u_rv_core.state$364' from module `\\tt_um_rv_core' using `auto' encoding:
  mapping auto encoding to `one-hot` for this FSM.
  00 -> ---1
  10 -> --1-
  01 -> -1--
  11 -> 1---
Recoding FSM `$fsm$\\q$9' from module `\\top' using `binary' encoding:
  0 -> 0
  1 -> 1
"""


def test_recoded_fsms_reads_the_map_and_skips_an_identity_one():
    [fsm] = leclib.recoded_fsms(RECODE_LOG)
    assert fsm.register == "u_rv_core.state" and fsm.encoding == "auto"
    assert fsm.old == ["00", "10", "01", "11"]
    assert fsm.new == ["---1", "--1-", "-1--", "1---"]


def test_abc_verdict_passes_only_a_proof():
    """ABC can only pass a link: a difference or no answer goes to sat."""
    assert leclib.abc_verdict("Networks are UNDECIDED after the new CEC engine."
                              "  Time = 1.17 sec\nCalling the old CEC engine."
                              "\nNetworks are equivalent.  Time = 1.18 sec\n")
    assert not leclib.abc_verdict("Networks are NOT EQUIVALENT.  Time = 0.01 sec")
    assert not leclib.abc_verdict("Networks are UNDECIDED.  Time = 9.00 sec")
    assert not leclib.abc_verdict("")


def test_miter_verdict_reads_sat_and_names_each_differing_point():
    shown = """\
      Signal Name                 Dec        Hex             Bin
      \\in_ui_in                    2          2        00000010
      \\gold_q.D                     1          1               1
      \\gate_q.D                     0          0               0
      \\cmp_q.D                      0          0               0
      \\cmp_uo_out                   1          1               1
"""
    assert leclib.miter_verdict("no model found: SUCCESS")[0] == "equal"
    assert leclib.miter_verdict("timeout")[0] == "unknown"
    status, differ, cex = leclib.miter_verdict(
        shown + "\nSAT proof finished - model found: FAIL!\n")
    assert status == "different" and differ == ["q.D"]
    assert cex["q.D"]["gold"] == "1" and cex["q.D"]["gate"] == "0"
    assert cex["q.D"]["inputs"] == {"ui_in": "2"}


@pytest.mark.parametrize("out,rc,want", [
    ("[info] No difference was found.", 0, "equal"),
    ("[info] Difference was found. Please refer", 0, "different"),
    ("[error] Workflow failed: LEC boundary mismatch", 1, "inconclusive"),
    ("", 139, "inconclusive"),
])
def test_kepler_exit_code_says_nothing_its_log_does(out, rc, want):
    assert leclib.kepler_verdict(out, rc)["verdict"] == want


def test_no_hardened_run_is_a_refusal(tmp_path):
    (tmp_path / "spec").mkdir()
    shutil.copy(fx.RUNG / "spec.yaml", tmp_path / "spec" / "spec.yaml")
    with pytest.raises(checklib.CheckError, match="harden gate has not run"):
        run(tmp_path)


def test_gate_is_registered_and_invalidated():
    import yaml
    gates = yaml.safe_load((REPO / "engine/reference/gates.yaml").read_text())
    inval = (REPO / "engine/reference/invalidation.yaml").read_text()
    assert "lec" in str(gates) and re.search(r"^\s+lec:\s+\[harden, rtl", inval,
                                             re.M)


# ---- real tools: counter8 --------------------------------------------------

@pytest.mark.slow
def test_untouched_reference_passes_on_both_links_and_engines(ws):
    p = run(ws)
    assert p["status"] == "pass", p["violations"]
    assert p["cross_check"]["verdict"] == "equal"
    for link in ("rtl_vs_synth", "synth_vs_final"):
        facts = p["links"][link]
        assert facts["key_points"] == 10      # count[7:0], 2 pad reset sync
        assert facts["proved"] == facts["compared"] > 8
        assert facts["prover"] == "abc cec"
        assert facts["constants"] == {} and facts["fsm"] == []
        assert facts["unmatched"] == {"gold": [], "gate": []}


@pytest.mark.slow
def test_stray_gate_passes_glsim_and_fails_lec_naming_the_flop(ws):
    """The issue's fault: visible tests stay green, and only a proof sees it."""
    plant = _plant_module()
    final = fx.final_path(ws)
    edited, flop = plant.stray_cone(final.read_text(encoding="utf-8"))
    final.write_text(edited, encoding="utf-8")
    # glsim's functional pass reads the powered netlist: the same netlist with
    # the tile's power ports stands in for it.
    pnl = final.parent.parent / "pnl" / final.name.replace(".nl.v", ".pnl.v")
    pnl.parent.mkdir(exist_ok=True)
    pnl.write_text(re.sub(r"(module \S+\s*\()([^;]*\);)",
                          r"\1VPWR, VGND, \2\n  inout VPWR;\n  inout VGND;",
                          edited, count=1), encoding="utf-8")

    spec = speclib.load_spec(ws / "spec" / "spec.yaml")
    build = ws / "log" / "gl"
    build.mkdir(parents=True)
    results, _ = check_glsim.run_pass(
        "functional", ws, spec, ws / "harden" / "runs" / "run" / "final",
        check_glsim._cell_sources(check_glsim._pdk_root()), build, True, None)
    assert results and all(r["passed"] for r in results.values())

    p = run(ws)
    assert kinds(p) == ["lec_not_equivalent"]
    assert points(p, "synth_vs_final") == {flop: "data"}
    assert p["links"]["synth_vs_final"]["prover"] == "yosys sat"
    assert p["links"]["rtl_vs_synth"]["proved"] == \
        p["links"]["rtl_vs_synth"]["compared"]
    assert p["cross_check"]["verdict"] == "different"
    cex = p["violations"][0]["counterexample"]
    assert cex["gold"] != cex["gate"] and cex["inputs"]["ui_in[1]"] == "1"


@pytest.mark.slow
def test_inverted_d_in_synth_fails_both_links(ws):
    plant = _plant_module()
    synth = fx.synth_path(ws)
    text = synth.read_text(encoding="utf-8")
    m = plant.FLOP.search(text)
    edited = text[:m.start()] + m.group(1) + "lec_n)" + text[m.end():]
    at = edited.rfind("\n", 0, edited.index(fx.CELL)) + 1
    synth.write_text(
        edited[:at] + f"  wire lec_n;\n  {fx.CELL}inv_1 lec_i "
        f"(.I({m.group(2)} ), .ZN(lec_n));\n" + edited[at:], encoding="utf-8")
    p = run(ws)
    assert kinds(p) == ["lec_not_equivalent"]
    assert set(points(p, "rtl_vs_synth").values()) == {"data"}
    assert set(points(p, "synth_vs_final").values()) == {"data"}
    assert p["cross_check"]["verdict"] == "different"


def _flop_edit(ws: Path, old: str, new: str, pin_old: str, pin_new: str) -> str:
    final = fx.final_path(ws)
    text = final.read_text(encoding="utf-8")
    m = re.search(rf"{fx.CELL}{old}\s+(\S+)\s*\([^;]*?;", text, re.S)
    block = m.group(0)
    edited = block.replace(f"{fx.CELL}{old}", f"{fx.CELL}{new}")
    edited = edited.replace(f".{pin_old}(", f".{pin_new}(")
    final.write_text(text.replace(block, edited), encoding="utf-8")
    return m.group(1)


@pytest.mark.slow
def test_wrong_clock_edge_fails_where_kepler_cannot_see_it(ws):
    """Why eqy doesn't get the flops as they are: both tools pass this if it
    does (leclib's docstring). Pins it as a known limit of the second engine."""
    flop = _flop_edit(ws, "dffq_1", "dffnq_1", "CLK", "CLKN")
    p = run(ws)
    assert points(p, "synth_vs_final") == {flop: "clock"}
    assert p["cross_check"] == {"tool": "kepler-formal", "verdict": "equal",
                                "eqy": "different"}
    assert "lec_engines_disagree" not in kinds(p)


@pytest.mark.slow
def test_flop_gaining_a_reset_fails(ws):
    final = fx.final_path(ws)
    text = final.read_text(encoding="utf-8")
    m = re.search(rf"{fx.CELL}dffq_1\s+(\S+)\s*\(", text)
    final.write_text(text[:m.start()] + f"{fx.CELL}dffrnq_1 {m.group(1)} "
                     "(.RN(rst_n)," + text[m.end():], encoding="utf-8")
    p = run(ws)
    assert points(p, "synth_vs_final") == {m.group(1): "reset"}


@pytest.mark.slow
def test_renamed_flop_is_unmatched_and_fails_with_kepler_refusing(ws):
    final = fx.final_path(ws)
    text = final.read_text(encoding="utf-8")
    m = re.search(rf"{fx.CELL}dffq_1\s+(\S+)\s*\(", text)
    final.write_text(text.replace(f"dffq_1 {m.group(1)} (", "dffq_1 renamed ("),
                     encoding="utf-8")
    p = run(ws)
    assert p["status"] != "pass"
    assert p["links"]["synth_vs_final"]["unmatched"] == {
        "gold": [m.group(1)], "gate": ["renamed"]}
    assert p["cross_check"]["verdict"] == "inconclusive"


@pytest.mark.slow
def test_unmodelled_cell_is_a_finding_not_a_pass(ws):
    final = fx.final_path(ws)
    text = final.read_text(encoding="utf-8")
    final.write_text(text.replace(
        "endmodule", f"  {fx.CELL}icgtp_1 stray_icg (.CLK(clk), .E(ena), "
        ".TE(ena), .Q());\nendmodule", 1), encoding="utf-8")
    assert "lec_unmodelled_cell" in kinds(run(ws))


@pytest.mark.slow
def test_register_on_a_memory_read_keeps_its_name(tmp_path):
    """spi_fifo: folded into the read port, its flops have no name to match and
    a correct design fails."""
    ws = fx.build(tmp_path / "ws", REPO / "corpus" / "vde" / "spi_fifo")
    p = run(ws)
    assert p["status"] == "pass", p["violations"]
    assert p["links"]["rtl_vs_synth"]["unmatched"] == {"gold": [], "gate": []}


def _rung(tmp_path: Path, rtl: str) -> Path:
    rung = tmp_path / "rung"
    (rung / "rtl").mkdir(parents=True, exist_ok=True)
    (rung / "tb").mkdir(exist_ok=True)
    shutil.copy(fx.RUNG / "spec.yaml", rung / "spec.yaml")
    (rung / "rtl" / "counter8.v").write_text(rtl, encoding="utf-8")
    return rung


@pytest.fixture(scope="module")
def fsm_ws(tmp_path_factory) -> Path:
    tmp = tmp_path_factory.mktemp("fsm")
    return fx.build(tmp / "ws", _rung(tmp, FSM))


@pytest.fixture
def fsm_copy(fsm_ws, tmp_path) -> Path:
    copy = tmp_path / "ws"
    shutil.copytree(fsm_ws, copy, ignore=shutil.ignore_patterns("log"))
    return copy


def _synth_log(ws: Path) -> Path:
    return fx.synth_path(ws).parent / "yosys-synthesis.log"


@pytest.mark.slow
def test_recoded_fsm_passes_through_its_map_and_matches_the_unrecoded_run(
        fsm_copy, tmp_path):
    """The state register keeps its name and changes encoding: proved state by
    state through the map in synthesis's log, with no fsm_encoding needed."""
    assert "Recoding FSM" in _synth_log(fsm_copy).read_text(encoding="utf-8")
    p = run(fsm_copy)
    assert p["status"] == "pass", p["violations"]
    [fsm] = p["links"]["rtl_vs_synth"]["fsm"]
    assert fsm["applied"] and fsm["states"] == 4
    assert fsm["register"] == "u_counter8.state"
    assert p["links"]["synth_vs_final"]["fsm"] == []

    # The cross-check: kept as written, the same RTL passes the same way.
    rung = _rung(tmp_path / "none", FSM.replace(
        "reg [1:0] state", '(* fsm_encoding = "none" *) reg [1:0] state'))
    plain = run(fx.build(tmp_path / "none" / "ws", rung))
    assert plain["status"] == "pass", plain["violations"]
    assert plain["links"]["rtl_vs_synth"]["fsm"] == []
    for link in ("rtl_vs_synth", "synth_vs_final"):
        assert plain["links"][link]["proved"] == plain["links"][link]["compared"]


@pytest.mark.slow
def test_recoded_fsm_with_a_wrong_transition_fails_on_the_state(fsm_copy):
    rtl = fsm_copy / "rtl" / "counter8.v"
    rtl.write_text(rtl.read_text(encoding="utf-8").replace(
        "B: begin state <= C;", "B: begin state <= D;"), encoding="utf-8")
    p = run(fsm_copy)
    assert kinds(p) == ["lec_not_equivalent"]
    named = points(p, "rtl_vs_synth")
    assert named and all(k.startswith("u_counter8.state.recoded")
                         for k in named), named
    assert not points(p, "synth_vs_final")


@pytest.mark.slow
def test_wrong_encoding_map_fails_never_passes(fsm_copy):
    log = _synth_log(fsm_copy)
    text = log.read_text(encoding="utf-8")
    codes = re.findall(r"^  [01]+ -> ([01-]+)$", text, re.M)
    assert len(codes) == 4
    swapped = text.replace(f"-> {codes[1]}\n", "-> SWAP\n").replace(
        f"-> {codes[2]}\n", f"-> {codes[1]}\n").replace(
        "-> SWAP\n", f"-> {codes[2]}\n")
    assert swapped != text
    log.write_text(swapped, encoding="utf-8")
    p = run(fsm_copy)
    assert kinds(p) == ["lec_not_equivalent"]
    assert any(k.startswith("u_counter8.state") for k in points(p, "rtl_vs_synth"))


# counter8 with a flop that never leaves its reset value: synthesis drops it.
STUCK = """module counter8 (input wire clk, input wire rst, output reg [7:0] count);
  reg stuck;
  always @(posedge clk)
    if (rst) begin stuck <= 1'b0; count <= 8'd0; end
    else begin stuck <= stuck; count <= count + {7'd0, ~stuck}; end
endmodule
"""


@pytest.mark.slow
def test_constant_flop_is_proved_constant_and_passes(tmp_path):
    p = run(fx.build(tmp_path / "ws", _rung(tmp_path, STUCK)))
    assert p["status"] == "pass", p["violations"]
    facts = p["links"]["rtl_vs_synth"]
    assert facts["constants"] == {"u_counter8.stuck": 0}, facts
    assert facts["unmatched"] == {"gold": [], "gate": []}


@pytest.mark.slow
def test_flop_lost_from_the_netlist_fails_not_constant(ws):
    """A counter bit synthesis wrongly tied off: gold-only, not constant."""
    synth = fx.synth_path(ws)
    text = synth.read_text(encoding="utf-8")
    m = re.search(rf"{fx.CELL}dffq_1\s+(\S+)\s*\(([^;]*?)\);", text, re.S)
    q = re.search(r"\.Q\(([^)]*?)\s*\)", m.group(2)).group(1).strip()
    synth.write_text(text.replace(m.group(0), f"assign {q} = 1'b0;"),
                     encoding="utf-8")
    p = run(ws)
    assert kinds(p) == ["lec_not_equivalent"]
    facts = p["links"]["rtl_vs_synth"]
    assert facts["constants"] == {} and len(facts["unmatched"]["gold"]) == 1


@pytest.mark.slow
def test_trimmed_run_dir_refuses(ws):
    shutil.rmtree(fx.synth_path(ws).parent)
    with pytest.raises(checklib.CheckError, match="trimmed"):
        run(ws)


@pytest.mark.slow
def test_rtl_change_after_harden_fails_the_rtl_link(ws):
    rtl = ws / "rtl" / "counter8.v"
    rtl.write_text(rtl.read_text(encoding="utf-8").replace(
        "count + 8'd1", "count + 8'd3"), encoding="utf-8")
    p = run(ws)
    assert kinds(p) == ["lec_not_equivalent"]
    assert points(p, "rtl_vs_synth") and not points(p, "synth_vs_final")


def test_lec_result_records_against_its_own_stamp():
    """gate_inputs' first kind is the one state.py compares a report's stamp
    with. check_lec stamps ws/harden, so a lec listed under rtl could never be
    recorded, proved or not."""
    data = yaml.safe_load((REPO / "engine" / "reference" / "invalidation.yaml")
                          .read_text(encoding="utf-8"))
    assert data["gate_inputs"]["vde"]["lec"][0] == "harden"
