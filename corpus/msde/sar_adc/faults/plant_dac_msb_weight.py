"""Fault: the DAC's MSB weighs 3% more than 128 LSB (a capacitor mismatch
in a real array), so every code with bit 7 set sits ~4 LSB off and the
transfer has a step at mid-scale. The codes still come back and the bench
still passes every functional check; only the dynamic measure sees it:
`cosim` must fail it as `measure_out_of_bounds` on enob (~6.4 < 7.5)."""
from pathlib import Path


def plant(ws: Path) -> None:
    path = ws / "tb" / "sar_adc_ideal.sp"
    text = path.read_text(encoding="utf-8")
    before = " w7=128 "
    if before not in text:
        raise ValueError(f"{path}: no `{before.strip()}` to plant on")
    path.write_text(text.replace(before, " w7=131.84 ", 1), encoding="utf-8")
