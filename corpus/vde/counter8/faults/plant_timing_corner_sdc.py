"""Fault: a constraint only the slow corners carry (gates.yaml's timing row),
the PLL's own miss: its constraints picked a value per corner, and the
timing gate used to read LibreLane's written-back final/sdc, one step's
write_sdc of the constraints as they came out at the one corner that step
loaded, so the other corners were timed against that corner's numbers.
check_timing now evaluates the signoff SDC once per corner, with that
corner's name in ::env(_CURRENT_CORNER_NAME), as LibreLane's own signoff
STA does.

counter8's RTL and spec are untouched. The plant writes harden/
constraints.sdc with an output delay of 19.5 ns (of the 20 ns clock) on
every output at the ss corners only: no flop's clock-to-out fits in what
is left, so every ss corner misses setup. (An output delay, not a clock
uncertainty: harden's flow.sdc sets the uncertainty again after it
sources constraints.sdc.) The tt corners keep LibreLane's own
constraints, so harden still completes (its own checker counts only
TIMING_VIOLATION_CORNERS, *tt* on gf180mcuD, and its PnR steps load the
nom_tt corner) and the timing gate's per-corner OpenSTA re-check is what
catches it.

check_timing.py reads harden's own output on disk, so this fault runs
check_harden.run() itself first, as plant_timing.py does."""
from pathlib import Path

SDC = """\
# planted: a slow-corner-only output budget, picked per corner as LibreLane
# evaluates an SDC (::env(_CURRENT_CORNER_NAME) is the corner being timed)
if {[info exists ::env(_CURRENT_CORNER_NAME)]
    && [string match "*_ss_*" $::env(_CURRENT_CORNER_NAME)]} {
    set_output_delay 19.5 -clock [get_clocks] [all_outputs]
}
"""


def plant(ws: Path) -> None:
    spec = (ws / "spec" / "spec.yaml").read_text(encoding="utf-8")
    if "period_ns: 20\n" not in spec:
        raise RuntimeError(
            f"{ws / 'spec' / 'spec.yaml'}: expected counter8's 'period_ns: "
            "20' - the 19.5 ns budget below is sized against it")
    harden = ws / "harden"
    harden.mkdir(exist_ok=True)
    (harden / "constraints.sdc").write_text(SDC, encoding="utf-8")

    import check_harden
    payload, _out = check_harden.run(["--workspace", str(ws)])
    if payload.get("status") != "pass":
        raise RuntimeError(
            "plant_timing_corner_sdc.py: harden itself did not pass with "
            f"the slow-corner constraint (status {payload.get('status')!r}) "
            "- the fault needs a clean harden output to check timing "
            f"against: {payload.get('violations')}")
