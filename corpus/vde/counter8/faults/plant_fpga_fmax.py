"""Fault: a spec clock the FPGA cannot reach (gates.yaml's fpga row, #96).
counter8's RTL is untouched; spec.yaml's clock.period_ns shrinks from 20
to 2 (500 MHz), far above the roughly 140 MHz nextpnr reaches for it on
the iCE40UP5K, so the step fails with fpga_fmax_below_clock."""
from pathlib import Path


def plant(ws: Path) -> None:
    spec_path = ws / "spec" / "spec.yaml"
    text = spec_path.read_text(encoding="utf-8")
    if "period_ns: 20\n" not in text:
        raise RuntimeError(
            f"{spec_path}: expected a 'period_ns: 20' line to shrink, "
            "found none - has counter8's spec.yaml changed?")
    spec_path.write_text(text.replace("period_ns: 20\n", "period_ns: 2\n"),
                         encoding="utf-8")
