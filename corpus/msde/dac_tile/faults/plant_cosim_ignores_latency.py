"""Fault: a macro top's cosim bench that ignores the pad reset latency
(gates.yaml's msde cosim row, #208). The top is framed as the tile ships
it, so rst_n reaches the code register through the pad reset synchroniser;
this drops the bench's wait for latency(dut.digital), so its first load
lands while the register is still held in reset and code_mismatches leaves
its bound."""
from pathlib import Path

WAIT = """        for _ in range(latency(dut.digital)):
            await FallingEdge(dut.clk)
"""


def plant(ws: Path) -> None:
    path = ws / "tb" / "test_dac_tile_cosim.py"
    text = path.read_text(encoding="utf-8")
    if WAIT not in text:
        raise RuntimeError("plant_cosim_ignores_latency.py: the bench's "
                           "latency wait is not where it was")
    path.write_text(text.replace(WAIT, ""), encoding="utf-8")
