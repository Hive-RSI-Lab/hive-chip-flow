# cdc_multibit_unsafe (cdc)

A bus crosses into another clock domain with each bit synchronised on its own,
and the gate can't prove the bus gray-coded. Each bit settles on its own
cycle, so when two bits change together the receiver can read a value the
source never held (a binary counter going 0111 -> 1000 can read 1111).
Routes to `rtl`.

**Cheapest fix first:**
- A counter or pointer: send it gray-coded. Register the gray value in the
  source domain (`gray <= bin_next ^ (bin_next >> 1)`) and synchronise that
  register, not the binary one. The gate proves gray from the netlist
  (`buses` in the report says why a proof failed and gives an example step),
  so the gray register needs an async reset and a cone of influence small
  enough to explore. Declaring it gray doesn't count.
- Any other bus: hold the data stable and send one synchronised qualifier
  (a req/ack handshake, or a valid that the destination synchronises and uses
  as the load enable or mux select for the data). The data then crosses
  unsynchronised but `qualified`, and no longer fails.
- An async FIFO for a stream.

Bits that really are independent signals (separate buttons on one port) are
declared bit by bit under `ports` in `spec/cdc.yaml` (`ui_in[0]: async`), which
makes each bit its own bus. A waiver is the person's decision, never a fixer's.
