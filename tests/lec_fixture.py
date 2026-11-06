"""A counter8 "hardened" run for the lec gate's tests, with no harden: the
RTL through a real yosys synthesis, then the edits P&R makes, by text. The
run dir has the layout LibreLane leaves, so check_lec reads it as a real one."""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EDA = REPO / "bin" / "eda"
RUNG = REPO / "corpus" / "vde" / "counter8"
CELL = "gf180mcu_fd_sc_mcu7t5v0__"


def _wrapper(ws: Path) -> str:
    import speclib
    import ttlib
    return ttlib.wrapper_name(speclib.load_spec(ws / "spec" / "spec.yaml"))


def synth_path(ws: Path) -> Path:
    return (ws / "harden" / "runs" / "run" / "06-yosys-synthesis" /
            f"{_wrapper(ws)}.nl.v")


def final_path(ws: Path) -> Path:
    return (ws / "harden" / "runs" / "run" / "final" / "nl" /
            f"{_wrapper(ws)}.nl.v")


def pnr_edits(netlist: str) -> str:
    """What P&R does that must not change function: tap, fill, decap and an
    antenna diode (no output), a tree of clock buffers, a hold delay and a
    resized gate."""
    clk_pins = re.findall(r"\.CLK\(clk\)", netlist)
    assert clk_pins, "no flop on clk in the synth netlist"
    text = netlist.replace(".CLK(clk)", ".CLK(clknet_leaf)")
    # Declared ahead of the first use: kepler-formal refuses a wire after it.
    first = re.search(rf"^\s*{CELL}", text, re.M).start()
    text = (text[:first] + "  wire clknet_0;\n  wire clknet_leaf;\n" +
            text[first:])
    extra = f"""
  {CELL}clkbuf_8 clkbuf_0 (.I(clk), .Z(clknet_0));
  {CELL}clkbuf_16 clkbuf_leaf (.I(clknet_0), .Z(clknet_leaf));
  {CELL}antenna ANTENNA_1 (.I(clk));
  {CELL}filltie TAP_1 ();
  {CELL}endcap PHY_1 ();
  {CELL}fill_1 FILLER_1 ();
  {CELL}fillcap_4 FILLER_2 ();
endmodule"""
    return text.replace("endmodule", extra, 1)


def build(ws: Path, rung: Path = RUNG) -> Path:
    """The workspace with its run dir. Needs the eda image."""
    import check_synth
    import speclib
    import ttlib
    (ws / "spec").mkdir(parents=True)
    shutil.copy(rung / "spec.yaml", ws / "spec" / "spec.yaml")
    shutil.copytree(rung / "rtl", ws / "rtl")
    shutil.copytree(rung / "tb", ws / "tb")
    spec = speclib.load_spec(ws / "spec" / "spec.yaml")
    (ws / "harden").mkdir()
    top = ttlib.wrapper_name(spec)
    wrapper = ws / "harden" / f"{top}.v"
    wrapper.write_text(ttlib.generate_tt_wrapper(spec), encoding="utf-8")
    liberty = check_synth.liberty_path(check_synth.toolchain_root(),
                                       check_synth.choose_std_cell(spec))
    synth = synth_path(ws)
    synth.parent.mkdir(parents=True)
    sources = " ".join(str(f) for f in check_synth.collect_sources(ws))
    script = ws / "fixture.ys"
    script.write_text(f"""\
read_verilog -sv -DSYNTHESIS {sources} {wrapper}
synth -flatten -top {top}
dfflibmap -liberty {liberty}
abc -liberty {liberty}
hilomap -singleton -hicell {CELL}tieh Z -locell {CELL}tiel ZN
splitnets
opt_clean -purge
write_verilog -noattr {synth}
""", encoding="utf-8")
    # The log is where LibreLane keeps it: check_lec reads FSM maps there.
    subprocess.run([str(EDA), "yosys", "-q", "-l",
                    str(synth.parent / "yosys-synthesis.log"), "-s",
                    str(script)], check=True, capture_output=True)
    final = final_path(ws)
    final.parent.mkdir(parents=True)
    final.write_text(pnr_edits(synth.read_text(encoding="utf-8")),
                     encoding="utf-8")
    return ws


def swap_cell(text: str, inst: str, new_type: str) -> str:
    """Change an instance's type, its pins left as they are."""
    out, n = re.subn(rf"\b{CELL}\w+(\s+{re.escape(inst)}\s*\()",
                     rf"{CELL}{new_type}\1", text)
    assert n == 1, f"no single instance {inst}"
    return out
