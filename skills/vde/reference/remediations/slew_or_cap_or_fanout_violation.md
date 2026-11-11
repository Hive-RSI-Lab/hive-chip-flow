# slew_or_cap_or_fanout_violation (timing)

OpenSTA flagged a net over its slew, capacitance or fanout limit - a
signal-integrity/drive-strength concern LibreLane's own sizing/buffering
did not resolve. Routes to `harden`.

**Cheapest fix first:** re-run harden; this class often self-resolves
across LibreLane's own buffer-insertion passes on a rerun. If it
persists, the finding names the net and corner - a `must_keep` cell or an
unusually high-fanout net in the RTL (a shared reset/enable driving many
loads) is the usual real cause.

**Trap:** do not chase this purely inside harden/config.override.json - a genuinely
high-fanout net is often an RTL structuring problem (split the driver
across a buffer tree in the design, not just at the physical layer).

## The one pin is a clock buffer (`clkbuf_0_*/Z`, the clock tree root)

On a small tile (a 1x1 Tiny Tapeout GF180 tile with about 150 flops was
seen) `timing` can fail at every corner on a single pin: the clock tree's
root buffer, with a max fanout over the limit (16 against 10) and a max
capacitance over it (0.242 against 0.2 pF). Setup and hold are fine.
TritonCTS built the H-tree down to its fourth level but, the die being
small, placed no buffers on levels 1 to 3, so the root drives all 16
level-4 buffers directly. `repair_design` never touches clock nets, so
re-running harden does not change it, and nothing in the RTL is the cause.

What did not help: `CTS_SINK_CLUSTERING_SIZE` and
`CTS_SINK_CLUSTERING_MAX_DIAMETER` (the same tree came back).

Fix: in `harden/config.override.json` set `CTS_DISTANCE_BETWEEN_BUFFERS`
(microns) low enough that buffers go in on the inner levels; 40 gave 63
clock buffers, 6 per path, and no fanout or capacitance violation on that
tile. A smaller block (37 flops) never hit it. Then re-harden
(`fix-timing` with `harden_config_edit`). The first harden after you add or
change the override may be recorded stale, because `config.json` is
regenerated while the gate runs; run harden once more and it records clean.
