# device_overstress (pex_sim)

The block has `layout_ref/<block>_stress.yaml`, and a gate pair of a MOS
device or MOS capacitor left the allowance in a transient of the extracted
netlist. The finding names the device, the pair, the corner, the deck and the
bound broken. The allowance is in `engine/reference/stress_limits.yaml`.

GF180MCU DRM 14.1.4.2 sets 3.63 V as the DC maximum between the gate and any
other FET node, and allows the gate node to exceed it periodically, depending
on duty cycle, without publishing a number. The flow's numbers are its own,
chosen on principle and not fitted to any macro:

| excess | peak over 3.63 V | time over |
|---|---|---|
| repeating, in a declared window | 100 mV | 2% of the worst cycle spent more than 50 mV over, and that cycle's average under 3.63 V |
| one-off edge (enable, start-up) | 300 mV | 1 us per event |

The 50 mV share band is a provisional project number, to be checked against
the PDK documents; where they disagree, the documents win. Time inside the
band still counts toward the peak and the cycle average, and every device
that passes only because of the band is listed under `stress` as
`share_band_only`, with its peak and both shares.

A one-off peak within 10 mV of its bound, either side, needs the deck
rerun finer before it is judged (`stress_needs_fine_repeat.md`).

Less than 1 mV over is not an excursion. An excursion still over when the run
ends fails, because nothing shows it ever ends.

Fix the circuit. A node that floats above the supply in a held state needs a
defined level there. Do not widen a bound, shorten a bench to miss the
excursion, or declare a window around something that does not repeat. Every
device that passes only through the allowance is listed under `stress` in the
gate's report, with peak and time, and that list is read at review.

## The sidecar

```yaml
decks:
  - name: lock                     # letters, digits, _ and -
    bench: pll_stress_lock_tb.cir  # beside the sidecar, in layout_ref/
    corners: [ff_125c_vp10]        # names from the spec's sweep
    timeout_s: 3600                # optional, else the gate's own
    fine_step_s: 1.0e-11           # optional: the repeat for a near peak
    repeating:                     # optional: a whole number of cycles
      - {name: cycles, start_s: 2.24e-6, stop_s: 3.12e-6, period_s: 80.0e-9}
```

A bench is the post-layout bench form: the tokens `{pdk}`, `{extracted}`,
`{corner}`, `{res_corner}`, `{mim_corner}`, `{temp_c}` and `{vdd}`, an `xdut`
instance of the top cell, a `.tran` line and no `.control`. Run it past its
last window: an excursion the run cuts off reads as held.
