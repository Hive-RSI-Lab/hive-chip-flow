# stress_needs_fine_repeat (pex_sim)

A gate pair's one-off peak in a stress run of the extracted netlist lies
within `near_bound_v` (10 mV, in `engine/reference/stress_limits.yaml`) of
the one-off peak bound, on either side, and the deck declares no fine-step
repeat. The finding names the device, the pair, the corner and the deck.

An edge that close to the bound is not settled by the bench's time step: the
same edge resolved more finely can land on the other side. So the run is
not judged until it has been repeated finer.

Add `fine_step_s` to the deck in `layout_ref/<block>_stress.yaml`:

```yaml
decks:
  - name: enable
    bench: pll_stress_enable_tb.cir
    corners: [ff_m40c_vp10]
    fine_step_s: 1.0e-11      # finer than the bench's own largest step
```

The gate then reruns that deck at that corner, and only when a device is
near the bound, with the `.tran` print step and largest step both set to
`fine_step_s` (stop and start times kept). It judges that deck and corner
by the repeat, so a device the repeat puts over the bound fails as
`device_overstress`. The report's `stress.fine_repeats` lists each repeat
with every near device's peak in both runs; `stress.needs_fine_repeat`
lists the ones still waiting.

A step no finer than the bench's own is refused. Do not move the bound or
shorten the bench to step out of the band: if the repeat fails, fix the
circuit.
