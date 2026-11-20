# fpga_netlist_not_equal (fpga)

The netlist `synth_ice40` wrote (`log/fpga/fpga_top.json`) does something the
RTL does not: proved with yosys's own ice40 cell models, the outputs the
finding names can differ from reset (sby `abc pdr` found a counterexample,
its trace under `log/fpga/equiv/miter/`). `unproven` also lists the internal
signals that did not match. Routes to `review`.

**Cheapest fix first:** re-run the gate. The netlist is rebuilt each run, so
a hand edit or a stale file under `log/fpga/` is gone. If it comes back, it
is a synthesis bug or an X in the RTL that yosys resolved one way: read the
RTL behind the first signal named, and look for an uninitialised register,
a `casez`/`x` default, or a missing reset.

**Trap:** never edit the generated files under `log/fpga/`, and don't add
attributes to the RTL to get past it. lec proves the GF180 netlist equal to
the same RTL; this proof is what ties the board prototype to that chip.
