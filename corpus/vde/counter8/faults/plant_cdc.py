"""Fault: a flop sampling another clock domain with no synchroniser
(gates.yaml's cdc row). Everything else about the design stays valid."""
from pathlib import Path


def plant(ws: Path) -> None:
    rtl = ws / "rtl" / "counter8.v"
    text = rtl.read_text(encoding="utf-8")
    text = text.replace("    input  wire       rst,\n",
                        "    input  wire       rst,\n"
                        "    input  wire       clk2,\n")
    text = text.replace("  always @(posedge clk) begin\n",
                        "  reg other, sampled;\n"
                        "  always @(posedge clk2) other <= ~other;\n"
                        "  always @(posedge clk) sampled <= other;\n"
                        "  always @(posedge clk) begin\n", 1)
    text = text.replace("count + 8'd1", "count + 8'd1 + sampled")
    rtl.write_text(text, encoding="utf-8")
    spec = ws / "spec" / "spec.yaml"
    spec.write_text(spec.read_text(encoding="utf-8").replace(
        "domains: [clk]", "domains: [clk, clk2]\n  primary: clk"),
        encoding="utf-8")
