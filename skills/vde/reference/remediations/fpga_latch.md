# fpga_latch (fpga)

yosys inferred a latch while synthesising for the iCE40. Its logic cells
have no latch, so the latch is built from a LUT that feeds itself: an
untimed loop. Routes to `rtl`.

**Cheapest fix first:** the finding names the signal. Give it a value on
every path of its `always @*` (a default assignment at the top, or the
missing `else`/`default:`), or make it a clocked register.

**Trap:** `lint`'s LATCH finding is the same defect one stage earlier; if
lint passed, check for a waiver that hid it.
