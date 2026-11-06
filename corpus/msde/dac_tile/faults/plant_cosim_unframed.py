"""Fault: a macro top's cosim bench that instantiates the bare digital
block instead of its TT frame (#208). The bench would then run without the
pad reset synchroniser the tile ships with; check_cosim refuses it by name
(cosim_top_unframed) rather than record a verdict on a top that is not the
one taped out."""
from pathlib import Path

FRAMED = "  tt_frame_dac_tile_digital digital ("


def plant(ws: Path) -> None:
    path = ws / "tb" / "dac_tile_cosim_top.v"
    text = path.read_text(encoding="utf-8")
    if FRAMED not in text:
        raise RuntimeError("plant_cosim_unframed.py: the cosim top's frame "
                           "instance is not where it was")
    path.write_text(text.replace(FRAMED, "  dac_tile_digital digital ("),
                    encoding="utf-8")
