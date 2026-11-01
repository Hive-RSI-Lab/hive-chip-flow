"""Fault: an async reset released through a one-flop "synchroniser" (gates.yaml's
cdc row covers reset release too). Reference with a 2-flop sync passes the
gate; this is the same with the second flop removed."""
from pathlib import Path


def plant(ws: Path) -> None:
    rtl = ws / "rtl" / "counter8.v"
    text = rtl.read_text(encoding="utf-8")
    text = text.replace(
        "  always @(posedge clk) begin\n",
        "  reg s1;\n"
        "  always @(posedge clk or posedge rst)\n"
        "    if (rst) s1 <= 1'b1; else s1 <= 1'b0;\n"
        "  always @(posedge clk or posedge s1) begin\n", 1)
    text = text.replace("if (rst)\n      count", "if (s1)\n      count")
    rtl.write_text(text, encoding="utf-8")
