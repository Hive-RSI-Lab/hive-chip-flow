# bench_bound_pad_mismatch

A `tb/*.bounds.json` entry scores a measure in a different ua pad condition
(`pad`: `both`, `bare` or `pad`) than the spec measure it implements.
spec.yaml is the contract, so the bench moves to match it; never edit the
spec to match the bench.

Triage it to the bench-writer in WORK-ORDER MODE, which copies the spec
measure's `pad` (default `both`) into the sidecar bound.

**Trap:** a bench bound marked `bare` for a measure the spec scores through
the pad hides the pad from the gate. That is the case this check exists for.
