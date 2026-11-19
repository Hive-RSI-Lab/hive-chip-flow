# fpga_comb_loop (fpga)

yosys `synth_ice40` found a feedback path with no register in it. An FPGA
cannot time such a loop; the ASIC synth gate would flag it too, but there
ABC breaks the loop and the run finishes, so it is easy to miss. Routes to
`rtl`.

**Cheapest fix first:** the finding names the module. Trace the signals of
its `assign`/`always @*` chains back to their own inputs: the loop is a
signal that, directly or through one wire, helps compute itself. Put a
register in the loop or remove the feedback.

**Trap:** the module named may be `fpga_top` as well as your own: the loop
is flattened into the board top. Fix it in `rtl/`, never in the generated
files under `log/fpga/`.
