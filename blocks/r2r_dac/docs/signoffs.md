# R-2R DAC sign-offs

The owner's answers to the design flow's human checkpoints, recorded as given. Each H2 was presented by the flow on `track/r2r-dac` with a challenge code and the digest of what it covered, and both are copied here from that branch's `state.json` files.

Since [#7](https://github.com/ihsan-sa/ece298a-r2r-dac/pull/7) brought that branch onto main, the flow's own `state.json` files (`blocks/r2r_dac/state.json`, `analog/state.json`, `digital/state.json`) also record these checkpoints with the answer below.

## H2: release sign-offs

Answer, in the asker's words (Slack C0C96C5EUTT/1791992460.038239, relayed to issue #8 by dispatch on 2026-10-14):

> approve H2-1452e6 H2-4da295 H2-a7bfe1

| checkpoint | block | question as presented | presented | digest | answer |
|---|---|---|---|---|---|
| H2-1452e6 | the tile (msde, `blocks/r2r_dac/state.json`) | sign off the assembled tile for release and the design document? | 2026-10-13T08:54:57 | `b7b420cc730085563da85ec07264f9808a2ca4a4328b47a98b7009934ea3be34` | approved |
| H2-4da295 | analog half (ade, `blocks/r2r_dac/analog/state.json`) | sign off this layout for integration into the tile? | 2026-10-13T06:20:09 | `f3066803d4c17bf482426edf8bfed3c9abf2559aa5176880877ae1972a904a90` | approved |
| H2-a7bfe1 | digital half (vde, `blocks/r2r_dac/digital/state.json`) | sign off the digital front end for integration with the waived ss/125C timing? | 2026-10-13T04:22:30 | `9d9c15af5d74dd8f0458bff7ea36c3b444512e59017cfc10bd2399c5772b8b48` | approved |

The analog half was presented twice; `H2-202830` (2026-10-13T06:18:48) was superseded by `H2-4da295` and wasn't answered.
