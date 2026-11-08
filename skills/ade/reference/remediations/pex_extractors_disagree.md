# pex_extractors_disagree (pex_sim)

magic and klayout_pex 2.5D extracted the same GDS and gave a net's total
C, or the coupling C between two nets the bench probes, more than 20% of
the smaller apart (or only one of them names the net). One extractor read
the layout wrong, so the bench on magic's netlist cannot be trusted.
Routes to review, not straight to the layout-fixer.

**Cheapest fix first:** the finding carries `magic_ff` and `kpex_ff`; the
report's `extractor_crosscheck` has every row, and kpex's netlist and log
(`log/pex_sim/kpex/`). Look at which way it went:

- magic lower on a net with no label of its own: extresist drops the
  coupling C of an unlabelled net. Check that `name_unlabelled_nets`
  labelled it and that the label landed on the net (the magic log), and
  put a text label on a drawing layer on that net in
  `layout/gen_<block>.py`;
- one extractor reads a layer or device differently (a resistor body, a
  gate, a cap plate): spot-check the net with FasterCap in the image, fix
  that extraction in the engine (`layoutlib`, `kpexlib`,
  `kpex_runner.py`), and add a test with a known answer (a parallel plate
  against eps*A/t).

**Trap:** never raise `kpexlib.TOLERANCE`, drop kpex, or skip the net
until you know which extractor is wrong. A gap past the spread of two
sound 2.5D models (about 13% on the corpus) is a real misread.
