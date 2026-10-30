"""Fault (expected to pass): an operating point ngspice reached only after
its dynamic gmin stepping gave up, the PLL's own false refusal (its tt_pss
bench passed or failed at random). ngspice's op search falls through
dynamic gmin -> true gmin -> source stepping, printing "<step> failed" for
each step that gives up and "<step> completed" for the one that
converges, and whether the first step fails at all can turn on netlist
element order (magic orders a PEX netlist differently between
extractions). sim_tt used to read any "gmin stepping failed" as an engine
error; simlib now counts it only when no later step of the same search
completed (simlib.gmin_unrecovered).

The mirror converges at once, so the bench prints the PLL run's own
sequence ahead of its run, word for word as ngspice wrote it: the gate
must read the op as converged and judge the measures, which are the
untouched reference's own. Refuses when the bench has no lone `run` line
to put it before, so a no-op edit can never pass as the fault."""
from pathlib import Path

SEARCH = """\
echo "Note: Starting dynamic gmin stepping"
echo "Warning: Dynamic gmin stepping failed"
echo "Note: Starting true gmin stepping"
echo "Note: True gmin stepping completed"
"""


def plant(ws: Path) -> None:
    p = ws / "tb" / "mirror_tb.cir"
    text = p.read_text(encoding="utf-8")
    if text.count("\nrun\n") != 1:
        raise RuntimeError(f"plant_sim_tt_gmin_recovered.py: {p} has no "
                           "single `run` line in its .control block")
    p.write_text(text.replace("\nrun\n", "\n" + SEARCH + "run\n"),
                 encoding="utf-8")
