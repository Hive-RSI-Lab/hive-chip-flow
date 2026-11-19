# fpga_fmax_below_clock (fpga)

nextpnr placed and routed the design on the board, but its longest path
cannot run at the spec clock (`clock.period_ns`). The message gives the
achieved Fmax and the spec clock. Routes to `rtl`.

**Cheapest fix first:** read `log/fpga/nextpnr.log` for the critical path
and add a pipeline register on it (a wide adder or comparator chain is the
usual cause), or split the logic across two clocks of work.

**Trap:** the iCE40 is much slower than the GF180 cells, so the ASIC
timing gate can pass at a clock the FPGA cannot reach. A longer
`clock.period_ns` is a spec edit: it changes the chip as well as the
prototype, so it is the person's call (hold 2), never a fixer's.
