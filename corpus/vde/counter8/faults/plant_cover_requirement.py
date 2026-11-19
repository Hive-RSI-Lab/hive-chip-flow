"""Fault: a requirement whose cover point no visible test ever hits
(gates.yaml's cover row, #80b) - REQ-HOLD names HOLD, a count that stays
at 5 for two clocks out of reset, which this counter never does."""
from pathlib import Path

import yaml

HOLD = """
module counter8_hold_cover (
    input wire       clk,
    input wire       rst,
    input wire [7:0] count
);
  HOLD: cover property (@(posedge clk) disable iff (rst)
                        count == 8'd5 ##1 count == 8'd5);
endmodule

bind counter8 counter8_hold_cover u_hold (.clk(clk), .rst(rst),
                                          .count(count));
"""


def plant(ws: Path) -> None:
    (ws / "cover").mkdir(exist_ok=True)
    (ws / "cover" / "counter8_hold_cover.sv").write_text(HOLD,
                                                         encoding="utf-8")
    spec_path = ws / "spec" / "spec.yaml"
    spec = yaml.safe_load(spec_path.read_text(encoding="utf-8"))
    spec["requirements"].append({
        "id": "REQ-HOLD", "check": "sim", "cover": ["HOLD"],
        "text": "count holds its value for a clock when asked to"})
    spec_path.write_text(yaml.safe_dump(spec, sort_keys=False),
                         encoding="utf-8")
