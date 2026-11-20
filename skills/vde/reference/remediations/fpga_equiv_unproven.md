# fpga_equiv_unproven (fpga)

The proof that the ice40 netlist equals the RTL did not finish: the signal
induction left signals unmatched (an FSM yosys recoded, say), and the pdr
proof of the outputs then ran out of the gate's own budget or ended
undecided. The message says which. Not a pass. Routes to `review`.

**Cheapest fix first:** rerun `check_fpga.py` with a larger
`--proof-budget` (seconds). A design with a large state space may need
minutes of pdr.

**Trap:** an unproven netlist is not a different one, and not an equal one
either. Don't remove the `fpga:` section to make the gate not applicable.
