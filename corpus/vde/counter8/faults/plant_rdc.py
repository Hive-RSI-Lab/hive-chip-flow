"""Fault: an async reset released through a one-flop "synchroniser" (gates.yaml's
cdc row covers reset release too). Reference with a 2-flop sync passes the
gate; this is the same with the second flop removed.

On a tile whose rst_n goes through the generated wrapper's pad reset
synchroniser, cdc analyses the wrapper and the block together, and the block's
rst is already synchronous to clk there: a one-flop stage behind it releases a
synchronous reset, which is no fault. A hand-rolled block synchroniser carries
the release only when rst_n reaches the block raw, so the plant also opts the
tile out of the pad synchroniser (spec.yaml pad_reset_sync off, with its
reason). cdc then checks the block alone with rst_n asynchronous, as it does
for any opted-out tile, and must catch the one-flop release."""
from pathlib import Path

OPT_OUT = ("pad_reset_sync:\n"
           "  mode: off\n"
           "  reason: \"the block synchronises its own reset release\"\n")


def plant(ws: Path) -> None:
    rtl = ws / "rtl" / "counter8.v"
    text = rtl.read_text(encoding="utf-8")
    for old in ("  always @(posedge clk) begin\n", "if (rst)\n      count"):
        if old not in text:
            raise RuntimeError(f"{rtl}: expected {old!r} in counter8's "
                               "synchronous-reset block - has the RTL changed?")
    text = text.replace(
        "  always @(posedge clk) begin\n",
        "  reg s1;\n"
        "  always @(posedge clk or posedge rst)\n"
        "    if (rst) s1 <= 1'b1; else s1 <= 1'b0;\n"
        "  always @(posedge clk or posedge s1) begin\n", 1)
    text = text.replace("if (rst)\n      count", "if (s1)\n      count")
    rtl.write_text(text, encoding="utf-8")

    spec = ws / "spec" / "spec.yaml"
    text = spec.read_text(encoding="utf-8")
    if "pad_reset_sync" in text:
        raise RuntimeError(f"{spec}: already declares pad_reset_sync")
    spec.write_text(text.rstrip("\n") + "\n" + OPT_OUT, encoding="utf-8")
