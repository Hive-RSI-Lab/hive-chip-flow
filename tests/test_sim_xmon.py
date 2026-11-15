"""engine/lib/xmonlib.py: the X/Z output monitor the sim and holdout gates
compile beside the design (#84). The plan() tests are hermetic; the
`slow` ones run real cocotb-on-Icarus through check_sim / check_holdout
(`eda python -m pytest`)."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import xmonlib  # noqa: E402


def _spec(ports: dict, **extra) -> dict:
    return {"top": "top", "clock": {"domains": ["clk"]},
            "ports": {n: {"dir": d, "width": w} for n, (d, w) in ports.items()},
            **extra}


PORTS = {"clk": ("input", 1), "rst": ("input", 1), "en": ("input", 1),
         "count": ("output", 4), "q": ("output", 4)}


def test_plan_finds_clock_and_active_high_reset_by_name():
    p = xmonlib.plan(_spec(PORTS))
    assert p["on"] and p["clock"] == "clk" and p["reset"] == "rst"
    assert p["active_high"] is True and p["watch"] == ["count", "q"]


def test_plan_active_low_reset_by_name_and_tt_pins():
    ports = dict(PORTS)
    del ports["rst"]
    ports["rst_n"] = ("input", 1)
    assert xmonlib.plan(_spec(ports))["active_high"] is False
    ports2 = dict(PORTS)
    del ports2["rst"]
    ports2["clear"] = ("input", 1)
    p = xmonlib.plan(_spec(ports2, tt_pins={"clear": "~rst_n"}))
    assert p["reset"] == "clear" and p["active_high"] is True


def test_plan_explicit_sim_reset_wins_and_is_validated():
    p = xmonlib.plan(_spec(PORTS, sim={"reset": {"port": "en",
                                                 "active": "low"}}))
    assert p["reset"] == "en" and p["active_high"] is False
    with pytest.raises(xmonlib.XmonError):
        xmonlib.plan(_spec(PORTS, sim={"reset": {"port": "nope",
                                                 "active": "high"}}))
    with pytest.raises(xmonlib.XmonError):
        xmonlib.plan(_spec(PORTS, sim={"reset": {"port": "rst"}}))


def test_x_ok_needs_an_output_port_and_a_reason():
    p = xmonlib.plan(_spec(PORTS, sim={"x_ok": {"q": "loads only on en"}}))
    assert p["watch"] == ["count"] and p["x_ok"] == {"q": "loads only on en"}
    with pytest.raises(xmonlib.XmonError, match="reason"):
        xmonlib.plan(_spec(PORTS, sim={"x_ok": {"q": "  "}}))
    with pytest.raises(xmonlib.XmonError, match="not an output"):
        xmonlib.plan(_spec(PORTS, sim={"x_ok": {"en": "an input"}}))


def test_no_reset_or_no_clock_turns_the_monitor_off_with_a_reason():
    ports = dict(PORTS)
    del ports["rst"]
    p = xmonlib.plan(_spec(ports))
    assert p["on"] is False and "sim.reset" in p["why"]
    ports = dict(PORTS)
    del ports["clk"]
    p = xmonlib.plan({"top": "top", "ports": {
        n: {"dir": d, "width": w} for n, (d, w) in ports.items()}})
    assert p["on"] is False and "clock" in p["why"]


def test_read_hits_names_the_running_test(tmp_path):
    log = tmp_path / "sim.log"
    log.write_text(
        "     0.00ns INFO  cocotb.regression  running test_top.test_a (1/2)\n"
        "CF_XMON port=q t_ps=25000 value=xxxx\n"
        "     9.00ns INFO  cocotb.regression  running test_top.test_b (2/2)\n"
        "CF_XMON port=q t_ps=95000 value=zzzz\n", encoding="utf-8")
    hits = xmonlib.read_hits(log)
    assert [(h["port"], h["test"], h["t_ps"]) for h in hits] == [
        ("q", "test_a", 25000), ("q", "test_b", 95000)]
    found = xmonlib.findings(log, "holdout", name_test=False)
    assert all("test_" not in v["msg"] and v["module"] is None
               for v in found)


# ---- real sim ---------------------------------------------------------------

# q is a register only `en` loads and no reset reaches: Icarus' X-optimism
# leaves it X, and a bench that never checks q passes.
RTL_UNRESET = """\
module top (input wire clk, input wire rst, input wire en,
            output reg [3:0] count, output reg [3:0] q);
  always @(posedge clk) count <= rst ? 4'd0 : count + 4'd1;
  always @(posedge clk) if (en) q <= count;
endmodule
"""

RTL_RESET = RTL_UNRESET.replace("if (en) q <= count;",
                                "q <= rst ? 4'd0 : (en ? count : q);")

SPEC = """\
top: top
clock: {domains: [clk]}
ports:
  clk: {dir: input, width: 1}
  rst: {dir: input, width: 1}
  en: {dir: input, width: 1}
  count: {dir: output, width: 4}
  q: {dir: output, width: 4}
requirements:
  - id: REQ-COUNT
    text: count counts up from 0 after reset
    check: sim
"""

TB = """\
import cocotb
from cocotb.clock import Clock
from cocotb.triggers import RisingEdge, FallingEdge

# req: REQ-COUNT
@cocotb.test()
async def test_counts(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    dut.en.value = 0
    dut.rst.value = 1
    await RisingEdge(dut.clk)
    await RisingEdge(dut.clk)
    await FallingEdge(dut.clk)
    dut.rst.value = 0
    for i in range(1, 6):
        await RisingEdge(dut.clk)
        await FallingEdge(dut.clk)
        assert int(dut.count.value) == i
"""


def _ws(tmp_path: Path, rtl: str, spec: str, sub: str = "tb") -> Path:
    ws = tmp_path / "ws"
    for d in ("rtl", "spec", sub, "log"):
        (ws / d).mkdir(parents=True, exist_ok=True)
    (ws / "rtl" / "top.v").write_text(rtl, encoding="utf-8")
    (ws / "spec" / "spec.yaml").write_text(spec, encoding="utf-8")
    name = "test_top.py" if sub == "tb" else "test_top_holdout.py"
    (ws / sub / name).write_text(TB, encoding="utf-8")
    return ws


def _run(mod, ws, capsys):
    code = mod.main(["--workspace", str(ws)])
    return code, json.loads(capsys.readouterr().out)


@pytest.mark.slow
def test_unreset_output_register_fails_sim_naming_the_port(tmp_path, capsys):
    import check_sim
    code, out = _run(check_sim, _ws(tmp_path, RTL_UNRESET, SPEC), capsys)
    assert code == 1, out
    xs = [v for v in out["violations"] if v["kind"] == "output_x"]
    assert [v["port"] for v in xs] == ["q"], out
    assert "output q" in xs[0]["msg"] and xs[0]["module"] == "test_counts"
    assert out["tests_passed"] == 1  # the bench alone would have passed
    assert out["x_monitor"]["on"] and out["x_monitor"]["reset"] == "rst"


@pytest.mark.slow
def test_reset_output_register_passes_sim(tmp_path, capsys):
    import check_sim
    code, out = _run(check_sim, _ws(tmp_path, RTL_RESET, SPEC), capsys)
    assert code == 0, out


@pytest.mark.slow
def test_x_ok_with_a_reason_lets_the_port_through(tmp_path, capsys):
    import check_sim
    spec = SPEC + "sim:\n  x_ok:\n    q: holds X until the first en\n"
    code, out = _run(check_sim, _ws(tmp_path, RTL_UNRESET, spec), capsys)
    assert code == 0, out
    assert out["x_monitor"]["watched"] == ["count"]


@pytest.mark.slow
def test_unreset_output_fails_holdout_without_naming_the_test(tmp_path,
                                                              capsys):
    import check_holdout
    ws = _ws(tmp_path, RTL_UNRESET, SPEC, sub="holdout")
    code, out = _run(check_holdout, ws, capsys)
    assert code == 1, out
    xs = [v for v in out["violations"] if v["kind"] == "output_x"]
    assert [v["port"] for v in xs] == ["q"]
    assert "test_counts" not in json.dumps(xs)
