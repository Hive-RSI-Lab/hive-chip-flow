"""seqequivlib (#85): optimise's sequential-equivalence proof of a trial
against the baseline RTL. The fast tests cover the miter and the port plan;
the slow ones run the real prover (yosys + sby `abc pdr`) on counter8: a
one-hot rewrite is proven equivalent, a rewrite that only changes the wrap
at 255 is rejected with a counterexample, both directly and through
optimise's trial loop."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import optimise  # noqa: E402
import seqequivlib  # noqa: E402

COUNTER8 = REPO / "corpus" / "vde" / "counter8"

# count as two one-hot nibbles: lo rotates every clock, hi when lo wraps.
# No adder, same count sequence - the proof's "equivalent" case.
ONE_HOT = """\
module counter8 (
    input  wire       clk,
    input  wire       rst,
    output wire [7:0] count
);
  reg [15:0] lo, hi;
  always @(posedge clk) begin
    if (rst) begin
      lo <= 16'd1;
      hi <= 16'd1;
    end else begin
      lo <= {lo[14:0], lo[15]};
      if (lo[15]) hi <= {hi[14:0], hi[15]};
    end
  end
  function [3:0] index(input [15:0] h);
    integer i;
    begin
      index = 4'd0;
      for (i = 0; i < 16; i = i + 1)
        if (h[i]) index = index | i[3:0];
    end
  endfunction
  assign count = {index(hi), index(lo)};
endmodule
"""

# 255 wraps to 1, not 0: any bench shorter than 256 clocks passes it
WRAP_TO_ONE = ("count <= count + 8'd1;",
               "count <= (count == 8'd255) ? 8'd1 : count + 8'd1;")


def counter8_spec() -> dict:
    return yaml.safe_load((COUNTER8 / "spec.yaml").read_text(encoding="utf-8"))


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def wrap_rtl() -> str:
    text = (COUNTER8 / "rtl" / "counter8.v").read_text(encoding="utf-8")
    assert WRAP_TO_ONE[0] in text
    return text.replace(*WRAP_TO_ONE)


def test_port_plan_and_miter_for_counter8():
    plan = seqequivlib.port_plan(counter8_spec())
    assert plan["clock"] == "clk" and plan["reset"] == "rst"
    assert plan["active_high"] is True and plan["outputs"] == {"count": 8}
    text, where = seqequivlib.miter_verilog(plan, 2)
    lines = text.splitlines()
    assert "wire rst = cf_armed ? cf_rst_free : 1'b1;" in text
    assert list(where.values()) == ["count"]
    assert "assert (cf_ref_count == cf_uut_count)" in lines[
        next(iter(where)) - 1]


def test_port_plan_skips_x_ok_outputs_and_refuses_inout():
    spec = counter8_spec()
    spec["ports"]["dbg"] = {"dir": "output", "width": 2}
    spec["sim"] = {"x_ok": {"dbg": "debug bus, undefined after reset"}}
    assert seqequivlib.port_plan(spec)["outputs"] == {"count": 8}
    spec["ports"]["pad"] = {"dir": "inout", "width": 1}
    with pytest.raises(seqequivlib.Refused, match="inout"):
        seqequivlib.port_plan(spec)


def test_a_spec_it_cannot_model_is_inconclusive_never_equivalent(tmp_path):
    spec = counter8_spec()
    spec["clock"]["domains"] = ["clk", "clk2"]
    rtl = [COUNTER8 / "rtl" / "counter8.v"]
    v = seqequivlib.prove(spec, "counter8", rtl, rtl, tmp_path / "w")
    assert v["verdict"] == "inconclusive"
    assert "several clock domains" in v["why"]


@pytest.mark.slow
def test_counter8_one_hot_rewrite_is_proven_equivalent(tmp_path):
    uut = write(tmp_path / "uut" / "counter8.v", ONE_HOT)
    v = seqequivlib.prove(counter8_spec(), "counter8",
                          [COUNTER8 / "rtl" / "counter8.v"], [uut],
                          tmp_path / "w", timeout_s=120)
    assert v["verdict"] == "equivalent", v


@pytest.mark.slow
def test_counter8_wrap_to_one_is_rejected_with_a_counterexample(tmp_path):
    uut = write(tmp_path / "uut" / "counter8.v", wrap_rtl())
    v = seqequivlib.prove(counter8_spec(), "counter8",
                          [COUNTER8 / "rtl" / "counter8.v"], [uut],
                          tmp_path / "w", timeout_s=120)
    assert v["verdict"] == "different", v
    assert v["port"] == "count"
    # 255 clocks counting up from 0, the 256th shows 1 instead of 0
    assert v["cycle"] == 256 and v["step"] == 258, v
    assert v["trace"] and Path(v["trace"]).is_file(), v


def git(ws: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(ws), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.mark.slow
def test_optimise_trial_keeps_one_hot_and_rejects_wrap(tmp_path, monkeypatch):
    """Through optimise's own trial loop, with the real prover and every
    other constraint and the metric faked to pass (the wrap trial passes
    any bench shorter than 256 clocks, so only seq_equiv can catch it)."""
    import faults
    ws = faults.make_scratch_workspace(tmp_path, COUNTER8, "vde", "counter8")
    git(ws, "init", "-q")
    git(ws, "add", "-A")
    git(ws, "-c", "user.name=t", "-c", "user.email=t@l", "commit", "-qm", "i")
    monkeypatch.setattr(optimise, "run_constraints", lambda ew, detail: {
        "lint": "pass", "sim": "pass", "formal": "pass"})
    trial = {"n": 0}

    def fake_metric(ew, ev, profile, with_power):
        trial["n"] += 1
        return {"area": 100.0 - trial["n"], "slack": 1.0, "power": 1e-3,
                "synth": "pass", "ports": "pass", "must_keep": "pass",
                "detail": []}
    monkeypatch.setattr(optimise, "run_metric", fake_metric)
    target = ws / "rtl" / "counter8.v"
    base_text = target.read_text(encoding="utf-8")
    optimise.run_rtl_start(["--workspace", str(ws), "--target",
                            "rtl/counter8.v", "--trials", "5"])

    target.write_text(wrap_rtl(), encoding="utf-8")
    bad, _ = optimise.run_trial(["--workspace", str(ws), "--note", "wrap"])
    assert bad["kept"] is False, bad
    assert bad["constraints"]["seq_equiv"] == "fail", bad
    assert any("output count, 256 clocks after reset" in d
               and "counterexample" in d for d in bad["detail"]), bad
    assert target.read_text(encoding="utf-8") == base_text

    target.write_text(ONE_HOT, encoding="utf-8")
    good, _ = optimise.run_trial(["--workspace", str(ws), "--note", "1hot"])
    assert good["kept"] is True, good
    assert good["constraints"]["seq_equiv"] == "pass", good
