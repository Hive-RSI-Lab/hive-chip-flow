#!/usr/bin/env python
"""check_top_stress.py - msde's top_stress gate: device overstress on the
assembled top's digital standard cells (goals: "Device overstress on the
top-level digital standard cells").

    check_top_stress.py --workspace MSDE_WS [--out FILE] [--jobs N]
                        [--timeout S]

top_erc proves each device's static range; this gate is the transient side.
A fast edge into a cell couples through its gate-drain capacitance and can
push an internal node past the rails for a moment, which CVC never sees. Every
standard-cell type the top uses is simulated alone, at transistor level, from
the PDK's own spice, and each device is scored with stresslib against its
class's limit in stress_limits.yaml (the 5 V `05v0` class for this library's
devices), with the same share, band and one-off rules as pex_sim's stage.

What one cell's run is:
  walk     every input state, visited in Gray order, and in each state every
           input toggled away and back: each input edge from each state. One
           edge per slot, a slot half the top's fastest clock period, so the
           run is a clock-rate cell and the share is per clock period.
  edges    full-swing ramps of EDGE_S (20 ps), the liberty's fastest
           characterised transition: faster than any edge the top delivers.
           The largest step is half an edge. A run with a one-off peak
           within near_bound_v of its bound, as pex_sim's stage does, or with
           any pair's peak within FINE_MARGIN_V of a bound it is held to, is
           run again at a twentieth of an edge and scored from that run.
  loads    each output at the lightest and the heaviest load any instance of
           the cell drives in the top: the SPEF's net capacitance (min bucket
           for the light bench, max for the heavy) plus the liberty input
           capacitance of every standard-cell pin on the net, and of every
           macro pin from the macro's own ff lib. A net that leaves the top
           is counted under `ports`: its off-chip load is not in the SPEF.
           An instance whose output is on no SPEF net drives nothing, so the
           light load is 0.
  corners  ff, fs and sf at -40 C and the supply +10%: analog/spec/spec.yaml's
           supply.vdd, the one top_erc uses, times 1.1.

The report lists every device that passed its class limit anywhere, with peak,
share and run (`over_limit`), and each cell's worst terminal pair against its
limit (`cells`). A device that breaks a bound is a device_overstress finding,
a junction past breakdown a junction_over_breakdown one, each naming how many
instances of the cell the top holds.

A cell's diodes, resistors and capacitors are listed under its
`not_scored` with the reason; any other element that is not a rated X device
is a refusal. A cell the PDK spice lacks, a signal pin the liberty does not
place, a sink pin with no capacitance on record, or a top with no clock
period is a refusal (exit 2): the gate would otherwise
score less than is taped out. The spec's macros are the analog side and are
not standard cells; pex_sim's stage scores them.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(SCRIPTS.parent / "lib"))
import boundary  # noqa: E402
import check_lvs  # noqa: E402
import check_top_erc  # noqa: E402
import check_top_harden  # noqa: E402
import checklib  # noqa: E402
import layoutlib  # noqa: E402
import speclib  # noqa: E402
import stresslib  # noqa: E402
import ttlib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "check_top_stress"
TOOL = "top_stress"
LIBRARY = "gf180mcu_fd_sc_mcu7t5v0"
LIBERTY = (f"libs.ref/{LIBRARY}/lib/{LIBRARY}__ff_n40C_3v60.lib")
# the spec key of a macro's lib at the same corner, which top_harden delivers
MACRO_LIB = "lib_ff_n40C_3v60"
# Each standard cell's power pins and the rail each sits on.
RAILS = {"VDD": "vdd", "VNW": "vdd", "VPW": "0", "VSS": "0"}
CORNERS = ("ff", "fs", "sf")
TEMP_C = -40
OVERSUPPLY = 1.1
EDGE_S = 20e-12     # the liberty's fastest transition index, 0.02 ns
LOADS = ("min", "max")
DEFAULT_TIMEOUT_S = 600
# the run's largest step, half an edge; a one-off peak within near_bound_v
# of its bound is run again at FINE_STEP_S, as pex_sim's stage does
MAX_STEP_S = EDGE_S / 2
FINE_STEP_S = EDGE_S / 20
# ours: a coarse peak this close to a bound it is held to is run again at
# FINE_STEP_S. The half-edge step was seen to read up to 55 mV low on a
# library cell's drain-source peak, so the margin sits above that.
FINE_MARGIN_V = 0.1
# element lines of a cell that hold no MOS device and are listed, not scored:
# a diode's reverse voltage is its net's swing against a rail, which the
# drain-bulk junction of the cell driving that net is already held to; a
# resistor or capacitor has no limit in stress_limits.yaml. Any other element
# letter is a refusal.
UNSCORED_ELEMENTS = {
    "d": "diode: its reverse voltage is the net's swing against a rail, "
         "scored on the drain-bulk junction of the cell driving the net",
    "r": "resistor: no limit in stress_limits.yaml",
    "c": "capacitor: no limit in stress_limits.yaml"}
MAX_INPUTS = 8      # 2^8 states x 17 slots: past that a cell needs its own plan


def parse_spef(text: str) -> list[dict]:
    """[{cap_pf, conns: [(kind, name, pin, direction, cell)]}] of each D_NET:
    kind `P` a top port, `I` an instance pin with its cell type. Only the
    total capacitance is read; the unit must be pF."""
    unit = re.search(r"^\*C_UNIT\s+(\S+)\s+(\S+)", text, re.M)
    if not unit or float(unit.group(1)) != 1.0 or unit.group(2).upper() != "PF":
        raise CheckError("the SPEF's *C_UNIT is not 1 PF: the gate reads "
                         "net capacitance in pF")
    names = dict(re.findall(r"^(\*\d+)\s+(\S+)\s*$",
                            text.split("*NAME_MAP", 1)[1].split("*D_NET", 1)[0],
                            re.M)) if "*NAME_MAP" in text else {}
    nets = []
    for block in re.split(r"^\*D_NET\s+", text, flags=re.M)[1:]:
        head, _, rest = block.partition("\n")
        total = float(head.split()[1])
        conn = rest.split("*CONN", 1)[1].split("*CAP", 1)[0] if "*CONN" in rest \
            else ""
        conns = []
        for line in conn.splitlines():
            tok = line.split()
            if not tok or tok[0] not in ("*P", "*I"):
                continue
            cell = tok[tok.index("*D") + 1] if "*D" in tok else None
            ref = tok[1]
            if tok[0] == "*I":
                inst, _, pin = ref.rpartition(":")
                conns.append(("I", names.get(inst, inst), pin, tok[2], cell))
            else:
                conns.append(("P", names.get(ref, ref), None, tok[2], None))
        nets.append({"cap_pf": total, "conns": conns})
    return nets


def liberty_pins(text: str, cell: str) -> dict:
    """{pin: {direction, capacitance_pf}} of a cell's signal pins."""
    head = re.search(rf'^\s*cell\s*\(\s*"?{re.escape(cell)}"?\s*\)', text, re.M)
    if not head:
        raise CheckError(f"the ff liberty has no cell {cell}")
    body = boundary._block_after(text, head.end(), r"^\s*cell\s*\(")
    pins = {}
    starts = list(re.finditer(r'^\s*pin\s*\(\s*"?([^")\s]+)"?\s*\)', body, re.M))
    for k, m in enumerate(starts):
        block = body[m.end():starts[k + 1].start() if k + 1 < len(starts)
                     else len(body)]
        direction = re.search(r"\bdirection\s*:\s*\"?(\w+)", block)
        cap = re.search(r"(?<![\w_])capacitance\s*:\s*([-+0-9.eE]+)", block)
        pins[m.group(1)] = {
            "direction": direction.group(1) if direction else None,
            "capacitance_pf": float(cap.group(1)) if cap else None}
    return pins


def library(pdk: Path) -> tuple[str, str]:
    """(the standard cells' spice, the ff liberty) of the PDK."""
    spice, liberty = pdk / check_lvs.PDK_SPICE_MODELS[0], pdk / LIBERTY
    for p in (spice, liberty):
        if not p.is_file():
            raise CheckError(f"the PDK has no {p.name}")
    return (spice.read_text(errors="replace"),
            liberty.read_text(errors="replace"))


def cell_subckt(library_text: str, cell: str) -> str:
    m = re.search(rf"^\.subckt\s+{re.escape(cell)}\s.*?^\.ends\b[^\n]*",
                  library_text, re.M | re.S | re.I)
    if not m:
        raise CheckError(f"the PDK standard-cell spice has no .subckt {cell}")
    return m.group(0) + "\n"


def signal_pins(cell: str, pins: list[str], lib: dict) -> tuple[list, list]:
    """(inputs, outputs) of a cell: every subckt pin is a rail or a liberty
    input or output."""
    ins, outs = [], []
    for pin in pins:
        if pin.upper() in RAILS:
            continue
        info = lib.get(pin)
        if info is None or info["direction"] not in ("input", "output"):
            raise CheckError(
                f"{cell} pin {pin} is neither a rail ({', '.join(RAILS)}) nor "
                f"a liberty input or output "
                f"({'missing' if info is None else info['direction']}): the "
                "gate cannot drive or load it")
        (ins if info["direction"] == "input" else outs).append(pin)
    if len(ins) > MAX_INPUTS:
        raise CheckError(f"{cell} has {len(ins)} inputs: an every-state walk "
                         f"past {MAX_INPUTS} needs its own plan")
    return ins, outs


def walk(ins: list[str], supply: float, slot: float) -> tuple[dict, int]:
    """({input: PWL points}, slots used): from all-low, each state in Gray
    order is set one input at a time (one edge a slot), then every input is
    toggled away and back. The slot count is even, so the run is a whole
    number of clock periods."""
    level = {p: 0 for p in ins}
    pts = {p: [(0.0, 0.0)] for p in ins}
    slot_no = 1

    def edge(pin: str) -> None:
        nonlocal slot_no
        t = slot_no * slot
        pts[pin] += [(t, level[pin] * supply), (t + EDGE_S, (1 - level[pin]) * supply)]
        level[pin] ^= 1
        slot_no += 1

    for k in range(2 ** len(ins)):
        gray = k ^ (k >> 1)
        for i, pin in enumerate(ins):
            if level[pin] != (gray >> i) & 1:
                edge(pin)
        for pin in ins:
            edge(pin)
            edge(pin)
    used = slot_no - 1
    if used % 2:
        used += 1
    return pts, max(used, 2)


def bench(cell: str, sub_name: str, pins: list[str], ins: list[str],
          outs: list[str], loads: dict, pdk: Path, corner: str,
          supply: float, slot: float) -> tuple[str, stresslib.Window, list[str]]:
    """(the deck, its scoring window, the xdut's outer nodes)."""
    pts, slots = walk(ins, supply, slot)
    start, stop = slot, slot * (1 + slots)
    outer = [RAILS.get(p.upper(), f"n_{p}".lower()) for p in pins]
    lines = [f"* top_stress {cell} {corner}",
             f".include '{pdk}/libs.tech/ngspice/design.ngspice'",
             f".lib '{pdk}/libs.tech/ngspice/sm141064.ngspice' {corner}",
             f".include '{sub_name}'",
             f".temp {TEMP_C}",
             f"vsupply vdd 0 {supply:.6g}"]
    for pin in ins:
        lines.append(f"v_{pin} n_{pin} 0 PWL(" + " ".join(
            f"{a:.9g} {b:.6g}" for a, b in pts[pin]) + ")")
    for pin in outs:
        lines.append(f"c_{pin} n_{pin} 0 {max(loads[pin], 0.0):.6g}p")
    lines.append(f"{stresslib.INST} {' '.join(outer)} {cell}")
    lines.append(f".tran {EDGE_S / 10:.3g} {stop + slot:.9g} 0 {MAX_STEP_S:.3g}")
    lines.append(".end")
    window = stresslib.Window("clock", start, stop, 2 * slot)
    return "\n".join(lines) + "\n", window, outer


def fastest_period_ns(spec: dict) -> float:
    """The shortest clock period the top spec names: clock.period_ns and any
    timing_notes entry's period_ns. None of them is a refusal."""
    found = []
    clock = spec.get("clock") or {}
    found.append(clock.get("period_ns"))
    for note in (spec.get("timing_notes") or {}).values():
        if isinstance(note, dict):
            found.append(note.get("period_ns"))
    found = [float(p) for p in found
             if isinstance(p, (int, float)) and not isinstance(p, bool) and p > 0]
    if not found:
        raise CheckError("the top spec names no clock.period_ns: the walk's "
                         "slot is half the fastest clock period")
    return min(found)


def sink_cap(sink_cell: str, pin: str, lib_text: str, macro_libs: dict,
             lib_cache: dict) -> float:
    """The input capacitance in pF of `pin` on a sink of an output's net: a
    standard cell from the PDK liberty, a macro from its own ff lib. A sink
    with no capacitance on record is a refusal, not a zero."""
    if sink_cell not in lib_cache:
        if sink_cell.startswith(LIBRARY + "__"):
            lib_cache[sink_cell] = liberty_pins(lib_text, sink_cell)
        elif sink_cell in macro_libs:
            path = macro_libs[sink_cell]
            if path is None or not path.is_file():
                raise CheckError(
                    f"macro {sink_cell} loads a standard-cell output and has "
                    f"no {MACRO_LIB} lib at {path}: its pin capacitance is "
                    "unknown - run the macro_lib gate and top_harden")
            lib_cache[sink_cell] = liberty_pins(
                path.read_text(errors="replace"), sink_cell)
        else:
            raise CheckError(f"a SPEF net ends on {sink_cell}, which is "
                             f"neither a {LIBRARY} cell nor a spec macro")
    cap = lib_cache[sink_cell].get(pin, {}).get("capacitance_pf")
    if cap is None:
        raise CheckError(f"the liberty gives no capacitance for {sink_cell} "
                         f"pin {pin}, which loads a standard-cell output")
    return cap


def cell_loads(cell: str, outs: list[str], instances: int, spefs: dict,
               lib_text: str, lib_cache: dict, macro_libs: dict) -> dict:
    """{output pin: {min, max, ports}} in pF over every instance of `cell`;
    `ports` counts the nets that also leave the top, whose off-chip load the
    SPEF does not hold."""
    out = {}
    for pin in outs:
        loads, ports = {}, 0
        for bucket in LOADS:
            seen = []
            for net in spefs[bucket]:
                if not any(k == "I" and c == cell and p == pin and d == "O"
                           for k, _n, p, d, c in net["conns"]):
                    continue
                total = net["cap_pf"]
                for kind, name, sink, direction, sink_cell in net["conns"]:
                    if kind == "P" and bucket == "max":
                        ports += 1
                    if kind != "I" or direction != "I":
                        continue
                    if sink_cell is None:
                        raise CheckError(f"the {bucket} SPEF names no cell "
                                         f"for sink {name}:{sink}")
                    total += sink_cap(sink_cell, sink, lib_text, macro_libs,
                                      lib_cache)
                seen.append(total)
            if bucket == "min":
                loads[bucket] = 0.0 if len(seen) < instances else min(seen)
            else:
                loads[bucket] = max(seen) if seen else 0.0
        out[pin] = {**loads, "ports": ports}
    return out


def _simulate(job: dict, stem: str, text: str):
    """One ngspice run of `text`: (times, raw store, raw path)."""
    out = job["out"]
    deck, raw = out / f"{stem}.cir", out / f"{stem}.raw"
    layoutlib.fresh(deck, raw)
    deck.write_text(stresslib.with_control(text, raw.name, job["vectors"],
                                           deck.name), encoding="utf-8")
    try:
        layoutlib.run_ngspice(deck, cwd=out, timeout=job["timeout"])
    except layoutlib.LayoutError as exc:
        if isinstance(exc.__cause__, subprocess.TimeoutExpired):
            raise CheckError(f"top_stress {stem}: " + speclib.timeout_hint(
                TOOL, job["timeout"])) from exc
        raise CheckError(f"top_stress {stem}: {exc}") from exc
    if not raw.is_file():
        raise CheckError(f"top_stress {stem}: ngspice wrote no {raw.name}")
    t, store = stresslib.read_binary_raw(raw)
    return t, store, raw


def run_cell(job: dict) -> dict:
    """One cell at one corner and load: its scored rows, its worst pair, and
    the fine-step repeat when a peak lies near a bound."""
    devices, limits, stem = job["devices"], job["limits"], job["stem"]
    pairs = stresslib.pair_list(devices, limits)
    t, store, raw = _simulate(job, stem, job["text"])
    rows = stresslib.score_run(t, store, devices, [job["window"]], limits)
    peaks = stresslib.pair_peaks(store["data"], store["columns"], pairs)
    near = stresslib.near_bound(rows, limits)
    close = near_limit(pairs, peaks, limits)
    repeat = None
    if near or close:
        del store
        raw.unlink()
        fine_text = stresslib.with_step(job["text"], FINE_STEP_S, stem)
        t, store, raw = _simulate(job, stem + "__fine", fine_text)
        rows = stresslib.score_run(t, store, devices, [job["window"]], limits)
        peaks = stresslib.pair_peaks(store["data"], store["columns"], pairs)
        repeat = {"step_s": FINE_STEP_S,
                  "one_off_near_bound": [
                      {"label": n["label"], "pair": n["pair"],
                       "coarse_peak_over_v": n["peak_over_v"]} for n in near],
                  "near_limit": close}
    k = int(np.argmax([float(p) - r["level"] for p, r in zip(peaks, pairs)]))
    worst = {"device": pairs[k]["dev"]["label"], "pair": pairs[k]["pair"],
             "class": pairs[k]["class"], "peak_v": float(peaks[k]),
             "limit_v": pairs[k]["level"]}
    del store
    raw.unlink()
    return {"rows": rows, "worst": worst, "repeat": repeat}


def unscored_elements(text: str, cell: str) -> list[dict]:
    """[{element, model, reason}] of the element lines in `cell`'s .subckt
    that are not X devices: listed in the report, never dropped. A letter
    UNSCORED_ELEMENTS does not name is a refusal."""
    out, inside = [], False
    for line in stresslib.logical_lines(text):
        tok = line.split()
        head = tok[0].lower()
        if head == ".subckt":
            inside = tok[1].lower() == cell.lower()
            continue
        if head == ".ends":
            inside = False
            continue
        if not inside or head.startswith(("x", ".")):
            continue
        reason = UNSCORED_ELEMENTS.get(head[0])
        if reason is None:
            raise CheckError(f"{cell} holds element {tok[0]}, a kind the gate "
                             "neither scores nor can account for")
        plain = [t for t in tok[1:] if "=" not in t]
        model = plain[-1].lower() if len(plain) > 2 else head[0]
        out.append({"element": tok[0], "model": model, "reason": reason})
    return out


def near_limit(pairs: list[dict], peaks, limits: dict) -> list[dict]:
    """The pairs whose coarse peak lies within FINE_MARGIN_V of a bound it is
    held to, either side: the class limit, the limit plus the repeating and
    one-off allowances for a gate pair, and the breakdown for a junction."""
    rep_v = limits["gate_oxide"]["repeating"]["peak_over_v"]
    one_v = limits["gate_oxide"]["one_off"]["peak_over_v"]
    out = []
    for pair, peak in zip(pairs, peaks):
        if pair["class"] == "gate":
            bounds = (pair["level"], pair["level"] + rep_v, pair["level"] + one_v)
        else:
            bounds = (pair["level"],) + ((pair["breakdown"],)
                                         if pair["breakdown"] else ())
        if any(abs(float(peak) - b) <= FINE_MARGIN_V for b in bounds):
            out.append({"label": pair["dev"]["label"], "pair": pair["pair"],
                        "coarse_peak_v": float(peak)})
    return out


def stress_cells(cells: dict, pdk: Path, supply: float, slot: float,
                 out: Path, jobs: int, timeout: float) -> tuple[list, dict]:
    """Run every cell of `cells` ({cell: {text, pins, ins, outs, loads}}) at
    every corner and load. (rows tagged with cell, corner and load,
    {cell: worst pair}, the fine-step repeats)."""
    limits = stresslib.load_limits()
    out.mkdir(parents=True, exist_ok=True)
    work = []
    for cell, info in sorted(cells.items()):
        devices, others = stresslib.parse_devices(info["text"], cell)
        info["others"] = others
        info["not_scored"] = unscored_elements(info["text"], cell)
        info["devices"] = len(devices)
        if others:
            raise CheckError(
                f"{cell} holds {', '.join(sorted(others))}, which is no "
                "device stress_limits.yaml rates: the gate scores flat cells "
                "of rated devices only")
        if not devices:
            continue
        stresslib.check_models(devices, limits, f"{cell}")
        for dev in devices:
            dev["label"] = f"{cell}/{dev['name']}"
        sub_name = f"{cell}.sp"
        (out / sub_name).write_text(info["text"], encoding="utf-8")
        for corner in CORNERS:
            for bucket in LOADS:
                loads = {p: v[bucket] for p, v in info["loads"].items()}
                text, window, outer = bench(cell, sub_name, info["pins"],
                                            info["ins"], info["outs"], loads,
                                            pdk, corner, supply, slot)
                mine = [dict(d) for d in devices]
                stresslib.vectors_of(mine, info["pins"], outer)
                work.append({"cell": cell, "corner": corner, "load": bucket,
                             "stem": f"{cell}__{corner}__{bucket}",
                             "out": out, "text": text, "window": window,
                             "devices": mine, "limits": limits,
                             "timeout": timeout,
                             "vectors": sorted({v for d in mine
                                                for v in d["vectors"] if v})})
    rows, worst, repeats = [], {}, []
    with ThreadPoolExecutor(max_workers=max(1, jobs)) as pool:
        for job, got in zip(work, pool.map(run_cell, work)):
            # a device name repeats across cells (X_i_2): the label keys it
            rows += [{**r, "device": r["label"], "cell": job["cell"],
                      "corner": job["corner"],
                      "deck": f"{job['load']} load"} for r in got["rows"]]
            if got["repeat"]:
                repeats.append({"cell": job["cell"], "corner": job["corner"],
                                "load": job["load"], **got["repeat"]})
            w = got["worst"]
            have = worst.get(job["cell"])
            if have is None or w["peak_v"] - w["limit_v"] > \
                    have["peak_v"] - have["limit_v"]:
                worst[job["cell"]] = {**w, "corner": job["corner"],
                                      "load": job["load"]}
    return rows, worst, repeats


def over_limit(rows: list[dict], counts: dict) -> list[dict]:
    """Every device pair over its class limit in any run, at its worst run:
    peak, share of a clock period, status."""
    best: dict = {}
    for r in rows:
        if r["class"] == "gate":
            peak = max(x["peak_v"] for x in r["rows"])
            share = max((x["share"] for x in r["rows"]
                         if x["kind"] == "repeating"), default=0.0)
            entry = {"status": r["status"], "share": share}
        else:
            peak = r["peak_v"]
            entry = {"status": "over_breakdown" if r["over_breakdown"]
                     else "over_reference", "time_over_s": r["time_over_s"]}
        key = (r["cell"], r["device"], r["pair"])
        have = best.get(key)
        rank = lambda st, pk: (st in ("fail", "over_breakdown"), pk)
        if have is None or rank(entry["status"], peak) > \
                rank(have["status"], have["peak_v"]):
            best[key] = {"cell": r["cell"], "device": r["label"],
                         "model": r["model"], "pair": r["pair"],
                         "class": r["class"], "limit_v": r["limit_v"],
                         "peak_v": peak, "corner": r["corner"],
                         "load": r["deck"], "instances": counts[r["cell"]],
                         **entry}
    return sorted(best.values(), key=lambda d: (-d["peak_v"], d["device"],
                                                d["pair"]))


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="msde block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    ap.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1),
                    help="ngspice runs at once (default: up to 4)")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S,
                    help="seconds for one cell's ngspice run")
    args = ap.parse_args(argv)

    ws = Path(args.workspace).resolve()
    topdir = ws / check_top_harden.TOP_DIR
    spec_path = topdir / "spec" / "spec.yaml"
    if not spec_path.is_file():
        raise CheckError(f"no assembled top at {topdir} - has the top_harden "
                         "gate run?")
    spec = speclib.load_spec(spec_path)
    top = ttlib.wrapper_name(spec)
    final_dir = topdir / "harden" / "runs" / "run" / "final"
    pnl = final_dir / "pnl" / f"{top}.pnl.v"
    spef_paths = {b: final_dir / "spef" / b / f"{top}.{b}.spef" for b in LOADS}
    analog_spec = ws / "analog" / "spec" / "spec.yaml"
    for label, p in (("powered netlist", pnl), ("analog spec", analog_spec),
                     *((f"{b} SPEF", s) for b, s in spef_paths.items())):
        if not p.is_file():
            raise CheckError(f"no {label} at {p} - has top_harden run?")
    vdd = (speclib.load_spec(analog_spec).get("supply") or {}).get("vdd")
    if isinstance(vdd, bool) or not isinstance(vdd, (int, float)) or vdd <= 0:
        raise CheckError(f"{analog_spec} has no positive supply.vdd")
    supply = float(vdd) * OVERSUPPLY
    period_ns = fastest_period_ns(spec)
    slot = period_ns * 1e-9 / 2

    macros = {m["cell"].lower() for m in spec.get("macros") or []}
    macro_libs = {m["cell"]: (topdir / m["files"][MACRO_LIB]
                              if (m.get("files") or {}).get(MACRO_LIB) else None)
                  for m in spec.get("macros") or []}
    design = check_top_erc.parse_powered_netlist(pnl.read_text(errors="replace"))
    counts: dict = {}
    for cell, _name, _conns in design["instances"]:
        if cell.lower() in macros:
            continue
        if not cell.startswith(LIBRARY + "__"):
            raise CheckError(f"{pnl.name} instantiates {cell}, which is "
                             f"neither a {LIBRARY} cell nor a spec macro")
        counts[cell] = counts.get(cell, 0) + 1
    if not counts:
        raise CheckError(f"{pnl.name} holds no standard cell: nothing to score")
    spefs = {b: parse_spef(p.read_text(errors="replace"))
             for b, p in spef_paths.items()}
    pdk = check_lvs._pdk_root() / ttlib.PDK_NAME
    library_text, lib_text = library(pdk)

    cells, lib_cache = {}, {}
    for cell, n in counts.items():
        text = cell_subckt(library_text, cell)
        pins = stresslib.subckt_pins(text, cell)
        signal = [p for p in pins if p.upper() not in RAILS]
        lib = liberty_pins(lib_text, cell) if signal else {}
        lib_cache[cell] = lib
        ins, outs = signal_pins(cell, pins, lib)
        cells[cell] = {"text": text, "pins": pins, "ins": ins, "outs": outs,
                       "loads": cell_loads(cell, outs, n, spefs, lib_text,
                                           lib_cache, macro_libs)}

    work = ws / "log" / TOOL
    rows, worst, repeats = stress_cells(cells, pdk, supply, slot, work, args.jobs,
                               args.timeout)

    where = str(pnl.relative_to(ws))
    violations = []
    for (_dev, pair), bad in sorted(stresslib.failures_by_pair(rows).items()):
        first = max(bad, key=lambda r: r["peak_v"] if r["class"] != "gate"
                    else max(x["peak_v"] for x in r["rows"]))
        kind = "device_overstress" if first["class"] == "gate" \
            else "junction_over_breakdown"
        detail = ("; ".join(first["reasons"]) if first["class"] == "gate"
                  else f"peak {first['peak_v']:.3f} V at {first['t_peak_s']:.4g} s, "
                       f"breakdown {first['breakdown_v']:g} V")
        runs = sorted({f"{r['corner']} / {r['deck']}" for r in bad})
        n = counts[first["cell"]]
        violations.append(checklib.violation(
            TOOL, "error", where, first["cell"], kind,
            [first["label"], pair, first["corner"], first["deck"]],
            f"{first['label']} {pair} ({first['model']}, limit "
            f"{first['limit_v']:g} V) at {first['corner']} / {first['deck']}: "
            f"{detail} (fails in {len(runs)} run(s): {', '.join(runs[:8])}; "
            f"{n} instance(s) in the top)", SCRIPT))

    limits = stresslib.load_limits()
    others: dict = {}
    for info in cells.values():
        for model, k in info["others"].items():
            others[model] = others.get(model, 0) + k
    facts = stresslib.summarise(rows, len(CORNERS) * len(LOADS) * sum(
        1 for c in cells.values() if c["devices"]), sum(
        c["devices"] for c in cells.values()), limits, others)
    payload = checklib.report(
        SCRIPT, pnl, violations, top=top, vdd=float(vdd), supply_v=supply,
        corners=list(CORNERS), temp_c=TEMP_C, edge_s=EDGE_S,
        clock_period_ns=period_ns, slot_s=slot,
        over_limit=over_limit(rows, counts),
        cells=[{"cell": c, "instances": counts[c], "inputs": i["ins"],
                "outputs": i["outs"], "loads_pf": i["loads"],
                "devices": i["devices"], "not_scored": i["not_scored"],
                "worst": worst.get(c)}
               for c, i in sorted(cells.items())],
        stress=facts, fine_repeats=repeats, report=f"log/{TOOL}")
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
