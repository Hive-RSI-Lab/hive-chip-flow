"""Fault: a flop whose clock-to-Q delay only the SDF knows about (gates.yaml's
glsim row, docs/design.md "### M4."). Hardens the untouched RTL first
(`glsim` reads harden's own output on disk, not anything faults.py records),
then rewrites one dffq's CLK->Q IOPATH in the nominal-corner SDF glsim
annotates to 0.7 of the spec's period_ns (14ns at 20ns). tb/ clocks at that
period and samples on the falling edge, half a period after the rising one,
so with the SDF applied that bit reads a clock late,
while the functional (zero-delay) pass stays green. A gate that runs its
"sdf" pass without really annotating (no -gspecify, or an SDF it skipped)
passes this fault; that is the bug it guards."""
import re
from pathlib import Path

# Of the clock period tb/ runs at (spec.yaml clocks.period_ns): past the
# falling-edge sample at 0.5, inside the period.
DELAY_FRACTION = 0.7
PERIOD_RE = re.compile(r"^\s+period_ns:\s*([0-9.]+)\s*$", re.M)
IOPATH_RE = re.compile(
    r'(\(CELLTYPE\s+"[^"]*__dffq_\d+"\)\s*\(INSTANCE\s+[^)]+\)\s*\(DELAY\s*'
    r'\(ABSOLUTE\s*\(IOPATH\s+CLK\s+Q\s+)\([^)]*\)\s*\([^)]*\)')


def plant(ws: Path) -> None:
    import check_glsim
    import check_harden
    import ttlib
    payload, _out = check_harden.run(["--workspace", str(ws)])
    if payload.get("status") != "pass":
        raise RuntimeError(
            f"plant_glsim_sdf.py: harden did not pass on the untouched "
            f"design (status {payload.get('status')!r}) - the fault needs a "
            f"clean harden output: {payload.get('violations')}")

    top = ttlib.wrapper_name({"top": "counter8"})
    corner = check_glsim.SDF_CORNER
    sdf = (ws / "harden" / "runs" / "run" / "final" / "sdf" / corner /
           f"{top}__{corner}.sdf")
    m = PERIOD_RE.search((ws / "spec" / "spec.yaml").read_text(encoding="utf-8"))
    if not m:
        raise RuntimeError("plant_glsim_sdf.py: no clocks.period_ns in spec.yaml")
    delay = DELAY_FRACTION * float(m.group(1))
    text = sdf.read_text(encoding="utf-8")
    d = f"({delay:.3f}:{delay:.3f}:{delay:.3f})"
    new, n = IOPATH_RE.subn(lambda m: f"{m.group(1)}{d} {d}", text, count=1)
    if n != 1:
        raise RuntimeError(f"plant_glsim_sdf.py: no dffq CLK->Q IOPATH in {sdf}")
    sdf.write_text(new, encoding="utf-8")
