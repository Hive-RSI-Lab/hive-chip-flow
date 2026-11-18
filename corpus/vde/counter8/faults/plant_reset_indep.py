"""Fault: a register with no reset whose x only reaches the output through
an if (gates.yaml's reset_indep row, #84). `stale` is never reset and only
ever holds its own value, so it keeps whatever it powered up with. Icarus
starts it at x and `if (x)` takes the else branch, so the counter counts and
`sim` passes; Verilator with every register at zero passes too. Powered up
non-zero, as a real chip can, the counter never counts, and reset_indep
fails with `reset_dependent`."""
from pathlib import Path

BUGGY = """\
module counter8 (
    input  wire       clk,
    input  wire       rst,
    output reg  [7:0] count
);
  reg [7:0] stale;
  always @(posedge clk) begin
    stale <= stale;
    if (rst)
      count <= 8'd0;
    else if (stale != 8'd0)
      count <= count;
    else
      count <= count + 8'd1;
  end
endmodule
"""


def plant(ws: Path) -> None:
    (ws / "rtl" / "counter8.v").write_text(BUGGY, encoding="utf-8")
