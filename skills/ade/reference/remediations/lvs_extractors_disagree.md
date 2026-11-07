# lvs_extractors_disagree (lvs)

netgen (on magic's extraction) and the PDK's klayout LVS deck gave
different verdicts on the same GDS against the same sized
`netlist/<block>.cir`. One of them extracted the layout wrong or read the
reference wrong, so neither verdict counts and the gate fails. Routes to
review, not straight to the layout-fixer.

**Cheapest fix first:** the finding carries both reports: `netgen.excerpt`
(netgen's log tail, `netgen.log` for all of it) and `klayout.findings`
(each unmatched net with its shapes, each unmatched device with both
sides' parameters; `klayout.lvsdb` for the full cross-reference). Read the
one that failed: it names the object the two disagree on. Then decide
which reading is right from the layout itself (the shape at that bbox) and
the netlist:

- the layout really is wrong there (a shape only one extractor sees as
  connecting, a device only one recognises): fix `layout/gen_<block>.py`;
- one extractor is wrong (a device class or parameter it cannot read, a
  layer it maps differently): fix that extraction or the reference
  translation in the engine (`layoutlib.klayout_reference`,
  `layoutlib.setup_spelling`), and add a test for it.

**Trap:** never drop one extractor, waive the finding, or edit the
netlist until the two agree. A split verdict is exactly the case the
second extractor is there to catch.
