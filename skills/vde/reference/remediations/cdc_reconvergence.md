# cdc_reconvergence (cdc)

Two or more signals from one source domain (or one async port) are
synchronised separately and meet again in the destination's logic. Each
synchroniser settles on its own cycle, so for a cycle the logic can see one
signal's new value next to the other's old one, a combination the source
never produced. Routes to `rtl`.

**Cheapest fix first:**
- Combine the signals in the source domain into one register and synchronise
  that single bit (an event, a valid, a state bit).
- Or synchronise one qualifier and send the rest as data it enables: a
  handshake, where the data is held stable and crosses `qualified`.
- If they are a bus that only ever changes one bit at a time, make them one
  gray-coded register so the multi-bit rule can prove it.

A waiver is the person's decision, never a fixer's.
