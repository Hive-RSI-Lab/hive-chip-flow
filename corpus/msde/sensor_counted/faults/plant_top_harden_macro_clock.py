"""Fault: a macro clock output the top buffers with no proof (#191).

Declares osc_out a clock the macro sources (macro_timing.clocks, a record
and a top SDC that follow it, macro_lib re-run) and clocks a small divider
from it, so CTS builds a tree on the net. This first runs a real top harden
on that design and requires the proof to hold with buffers CTS named for the
net, for a tree and not only for a net with one sink. Then the
digital side puts a PDK buffer it instantiated itself on the clock, a path
to the divider that no CTS name vouches for, and top_harden must fail with
macro_clock_buffer_unproven naming the pin."""
import json
import re
from pathlib import Path

PLANT = "plant_top_harden_macro_clock.py"
RTL = "sensor_counted_digital.v"
BUFFER = "gf180mcu_fd_sc_mcu7t5v0__buf_2"
RECORD = "reports/recorded/osc_clock.json"
MHZ = 12.5
SDC = """\
create_clock -name osc -period 80 [get_pins u_analog/osc_out]
set_clock_groups -asynchronous -group clk -group osc
"""


def declare(ws: Path) -> None:
    """osc_out as a clock of the macro, with what that owes: a passing
    record at every process, the lib view of that spec, and a top SDC."""
    import check_macro_lib
    analog = ws / "analog"
    record = analog / RECORD
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"status": "pass", "facts": {"measured": {
        corner: {"f_osc_mhz": MHZ}
        for corner in ("tt_025C_3v30", "ss_125C_3v00", "ff_n40C_3v60")}}}),
        encoding="utf-8")
    spec = analog / "spec" / "spec.yaml"
    text, n = re.subn(
        r"(\nmacro_timing:\n)",
        rf"\1  clocks:\n    osc_out: {{record: {RECORD}, measure: f_osc_mhz}}\n",
        spec.read_text(encoding="utf-8"))
    if n != 1:
        raise RuntimeError(f"{PLANT}: no macro_timing in {spec}")
    spec.write_text(text, encoding="utf-8")
    payload, _out = check_macro_lib.run(["--workspace", str(ws)])
    if payload.get("status") != "pass":
        raise RuntimeError(f"{PLANT}: macro_lib did not pass with osc_out a "
                           f"clock: {payload.get('violations')}")
    (ws / "top.sdc").write_text(SDC, encoding="utf-8")


def edit_rtl(ws: Path, *edits: tuple[str, str]) -> None:
    rtl = ws / "digital" / "rtl" / RTL
    text = rtl.read_text(encoding="utf-8")
    for old, new in edits:
        if text.count(old) != 1:
            raise RuntimeError(f"{PLANT}: the digital RTL no longer has {old!r}")
        text = text.replace(old, new)
    rtl.write_text(text, encoding="utf-8")


def divider(ws: Path, clock: str = "osc_out", extra: str = "") -> None:
    """A divider clocked from `clock`, its top bit what the synchroniser
    samples."""
    edit_rtl(ws, ("  // osc_en = en,", f"""\
{extra}  reg [3:0] osc_div;
  always @(posedge {clock} or negedge rst_n)
    if (!rst_n) osc_div <= 4'd0;
    else osc_div <= osc_div + 4'd1;

  // osc_en = en,"""),
             ("osc_sync0   <= osc_out;", "osc_sync0   <= osc_div[3];"))


def harden_clean(ws: Path) -> dict:
    """Run top_harden on the design with CTS alone on the clock: it must
    pass outright, the oscillator driving CTS's root buffer inside its slew
    and cap limits at every corner (#213)."""
    import check_top_harden
    payload, _out = check_top_harden.run(["--workspace", str(ws)])
    if payload.get("status") != "pass":
        raise RuntimeError(f"{PLANT}: top_harden did not pass with CTS alone "
                           f"on the clock: {payload.get('violations')}")
    proof = payload.get("clock_proof") or {}
    buffers = (proof.get("u_analog/osc_out") or {}).get("buffers")
    if not buffers:
        raise RuntimeError(f"{PLANT}: the run proved no CTS buffer on the "
                           f"clock, so it shows nothing: {proof}")
    return payload


def plant(ws: Path) -> None:
    # faults.macro_lib_first has run macro_lib on the untouched design
    declare(ws)
    divider(ws)
    harden_clean(ws)
    edit_rtl(ws, ("always @(posedge osc_out or negedge rst_n)",
                  "always @(posedge osc_buffered or negedge rst_n)"),
             ("  reg [3:0] osc_div;", f"""\
  wire osc_buffered;
  {BUFFER} u_osc_buffer (.I(osc_out), .Z(osc_buffered));
  reg [3:0] osc_div;"""))
