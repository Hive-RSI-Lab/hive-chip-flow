# unannotated_net (harden)

A driver with fanout has no parasitics in the SPEF, so signoff timing saw zero
wire on that net and its slack is optimistic. Routes to `harden`.

**Cheapest fix first:** the count is `timing__unannotated_net_filtered__count`.
Run the post-route STA step's report (`report_parasitic_annotation`) on the
final netlist and SPEF to see which nets. Usually the netlist and SPEF come from
different steps, or extraction skipped a net that a macro drives. Fix that and
re-harden.

**Trap:** the unfiltered `timing__unannotated_net__count` includes nets with no
fanout. It is often above zero and is not what this gate judges.
