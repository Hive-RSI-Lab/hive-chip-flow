# cosim_corner_no_result (cosim)

One corner run gave no result: its build failed, it passed `timeout_s`, or
it could not get its view (no SDF for the corner, a gate-level netlist that
the bench's Verilog top does not instantiate). The finding names
`<corner>/<view>` and gives the reason. The other corners keep their data.

**Cheapest fix first:** read the run's log under
`log/cosim_corners/<view>/<corner>/`. A run that timed out gets a larger
`corners.timeout_s` in `tb/cosim_bench.json`. A
`cosim_gl_netlist_not_instantiated` means the Verilog top does not
instantiate the module the netlist defines, so the top must use that name
on the gate-level run.

**Trap:** never drop the corner from `corners.list` or `corners.digital` to
make the finding go away. A corner that did not run is not a pass.
