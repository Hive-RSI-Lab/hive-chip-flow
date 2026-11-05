"""Fault: reset must be held for TWO consecutive pad clocks to take effect (a
register on the reset path itself, gated with the raw signal rather than
replacing it - `rst && rst_d`, not `rst_d` alone). Invisible to
tb/test_counter8.py, whose only reset test holds rst for several (3-4)
clocks at start-up - long enough for the two-clock requirement to be met
well before the test ever checks. Caught only by
holdout/test_counter8_holdout.py's one-clock mid-run reset (gates.yaml's
holdout row: "... which the visible tests do not look"): one clock is never
enough to clear count under this fault, so that test alone fails.

An earlier version of this fault gated on `rst_d` alone (dropping `rst`
entirely), which delays reset by one clock rather than requiring two held -
that also broke tb/'s own 3-4-clock reset tests (a one-clock delay is still
visible at 3-4 clocks), so the fault never demonstrated holdout catching
anything tb/ missed. faults.py's own harness (run_rung) now asserts the sim
gate still passes for every holdout fault, which would have caught that.

sim and holdout drive the block through the tile's pad (ttlib.write_sim_frame),
whose reset synchroniser holds the block in reset PAD_RESET_LATENCY (2) clocks
past the pad's release (engine/lib/padreset). The holdout's one-clock pad
pulse is therefore 3 clocks of block reset and tb/'s shortest (3 at the pad)
is 5, so a two-clock requirement no longer separates them: the fault requires
reset held 4 consecutive clocks at the block, i.e. two clocks at the pad."""
from pathlib import Path

BUGGY = """\
module counter8 (
    input  wire       clk,
    input  wire       rst,
    output reg  [7:0] count
);
  reg [2:0] rst_d;
  always @(posedge clk) begin
    rst_d <= {rst_d[1:0], rst};
    count <= (rst && (&rst_d)) ? 8'd0 : count + 8'd1;
  end
endmodule
"""


def plant(ws: Path) -> None:
    (ws / "rtl" / "counter8.v").write_text(BUGGY, encoding="utf-8")
