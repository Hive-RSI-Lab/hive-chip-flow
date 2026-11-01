"""Fault: gates.yaml's lec row. An ECO-style stray gate in final/nl, on a cone
the visible tests never sensitise: the first flop's D also goes high when
ui_in[2:1] are both 1, and no test of this rung drives them. glsim passes the
netlist; only a proof sees it. Runs on what harden left, so it needs a real
hardened run (faults.py's harden rungs). A literal and2-for-or2 swap has no
such cone here: abc maps this counter without and2, and the tests walk every
count."""
import re
from pathlib import Path

CELL = "gf180mcu_fd_sc_mcu7t5v0__"
FLOP = re.compile(rf"({CELL}s?dff\w+\s+\S+\s*\((?:[^;]*?)\.D\()\s*([^)]*?)\s*\)",
                  re.S)


def stray_cone(netlist: str) -> tuple[str, str]:
    """(netlist with the stray gate, the flop's instance)."""
    m = FLOP.search(netlist)
    if m is None:
        raise RuntimeError("plant_lec.py: no flop with a D pin in the netlist")
    instance = m.group(1).split()[1]
    first = netlist.index(CELL)
    start = netlist.rfind("\n", 0, first) + 1
    decl = "  wire lec_stray;\n  wire lec_d;\n"
    gates = (f"  {CELL}and2_1 lec_and (.A1(ui_in[1]), .A2(ui_in[2]), "
             f".Z(lec_stray));\n"
             f"  {CELL}or2_1 lec_or (.A1({m.group(2)} ), .A2(lec_stray), "
             f".Z(lec_d));\n")
    edited = netlist[:m.start()] + m.group(1) + "lec_d)" + netlist[m.end():]
    return edited[:start] + decl + gates + edited[start:], instance


def plant(ws: Path) -> None:
    import check_harden
    payload, _out = check_harden.run(["--workspace", str(ws)])
    if payload.get("status") != "pass":
        raise RuntimeError(f"plant_lec.py: harden did not pass: "
                           f"{payload.get('violations')}")
    nl = next((ws / "harden" / "runs" / "run" / "final" / "nl").glob("*.nl.v"))
    edited, _flop = stray_cone(nl.read_text(encoding="utf-8"))
    nl.write_text(edited, encoding="utf-8")
