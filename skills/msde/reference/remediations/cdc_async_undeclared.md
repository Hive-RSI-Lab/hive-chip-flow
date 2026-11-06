# cdc_async_undeclared (split)

`interface.yaml` has an a2d signal in domain `clk_free` (no clock, such as
a comparator or an oscillator output) that is not one of the digital
side's `clock.domains`, and `digital/spec/cdc.yaml` does not give it
`async` under `ports`. The digital side's `cdc` gate then takes that port
as synchronous to `clock.primary`, so it never checks the synchroniser.
The port shows under the cdc report's `assumed_ports`.

**Cheapest fix first:** run `eda python engine/scripts/split_cdc.py
--workspace <ws>`. It writes `<signal>: async` into
`digital/spec/cdc.yaml` and keeps the file's other ports and waivers. Then
re-run the digital side's `cdc` gate, which now checks the crossing. A
one-flop sample fails there as `cdc_unsynchronized`; fix that in the RTL
with a two-flop synchroniser.

**Trap:** do not settle this by giving the port a clock domain in
`cdc.yaml` (`<signal>: clk`). That is the same assumption the gate made,
written down, and `split` still fails it. If the signal really is
synchronous to the digital clock, `interface.yaml`'s `domain` is what is
wrong (see `domain_mismatch.md`).
