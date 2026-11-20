"""pexlib - a second extractor's read of a hardened block's critical nets
(issue #77).

LibreLane's SPEF comes from OpenRCX alone, and every timing number the
flow signs off rests on it. pexlib extracts the same routed DEF again with
magic (flattened, with the standard-cell LEF so cell pins are metal too)
and compares each named net's total capacitance and resistance with the
SPEF's:

  - C: the SPEF's *D_NET total (ground plus coupling) against the sum of
    every capacitor magic's ext2spice netlist hangs on the net (ground and
    coupling alike, so both totals count a coupling cap once per side).
  - R: the sum of the SPEF's *RES segments against magic's lumped
    resistance - each metal resistclass's area and perimeter in the .ext
    node line as squares, (P/2)^2/A, at the class's sheet resistance -
    plus the net's DEF vias at the tech LEF's per-cut RESISTANCE (magic's
    lumped figure has no vias; OpenRCX's -lef_res counts them).

What the two figures mean against each other was measured on counter8
(issue #77's comment) before this landed: magic counts the cell-pin metal
OpenRCX leaves to the liberty, so a short net reads high, and OpenRCX's
rules carry about twice magic's sidewall coupling, so a long net among
neighbours reads low. So a mismatch is a warning until the thresholds are
calibrated against a reference extraction, never yet an error.
"""
from __future__ import annotations

import math
import re
import shlex
import statistics
import subprocess
from pathlib import Path

C_TOL = 0.25          # |magic/OpenRCX - 1| above this is a C mismatch
R_TOL = 0.40          # ... and above this an R mismatch
# a difference this small cannot move a path's delay by anything STA
# reports, whatever the ratio (a 0.3 fF net read at 1.3 fF)
C_FLOOR_FF = 1.0
R_FLOOR_OHM = 10.0
# magic's resistclasses are in milliohms per square; the routing metals sit
# far below 1 ohm/sq, poly, diffusion and wells far above. Only metal is
# routed, and only routed metal is in OpenRCX's figure.
METAL_RS_MAX_MOHM = 1000
CRIT_NETS = 20
CRIT_MARK = "chipflow_xc_path"
CRIT_BEGIN = "chipflow_xc_begin"
CRIT_END = "chipflow_xc_end"
_SI = {"f": 1.0, "p": 1e3, "a": 1e-3, "n": 1e6, "u": 1e9}


class PexError(Exception):
    """The cross-check could not be made (a tool failed, a file is
    missing or unreadable): the caller refuses, never passes."""


# ----------------------------------------------------------------- STA side

def critical_paths_tcl(paths: int = CRIT_NETS) -> str:
    """OpenSTA Tcl printing, per setup and hold path (worst first, one per
    endpoint), its slack and the nets its pins sit on. A top-level port's
    pin has no net object, so the port's own name stands for its net."""
    return (
        f'puts "{CRIT_BEGIN}"\n'
        "set xc_rr $::sta_recovery_removal_checks_enabled\n"
        "set sta_recovery_removal_checks_enabled 0\n"
        "foreach xc_side {max min} {\n"
        "  foreach xc_pe [find_timing_paths -path_delay $xc_side "
        f"-sort_by_slack -group_path_count {paths} -endpoint_path_count 1] {{\n"
        "    set xc_nets {}\n"
        "    foreach xc_pt [get_property $xc_pe points] {\n"
        "      set xc_p [get_property $xc_pt pin]\n"
        "      set xc_n [get_nets -quiet -of_objects $xc_p]\n"
        '      if {$xc_n ne "NULL" && $xc_n ne ""} {\n'
        "        lappend xc_nets [get_full_name $xc_n]\n"
        "      } else {\n"
        "        lappend xc_nets [get_full_name $xc_p]\n"
        "      }\n"
        "    }\n"
        f'    puts "{CRIT_MARK} $xc_side [get_property $xc_pe slack] '
        '[lsort -unique $xc_nets]"\n'
        "  }\n"
        "}\n"
        "set sta_recovery_removal_checks_enabled $xc_rr\n"
        f'puts "{CRIT_END}"\n')


_CRIT_RE = re.compile(rf"^{CRIT_MARK} (max|min) (\S+) ?(.*?)\s*$", re.M)
_TCL_WORD = re.compile(r"\{([^}]*)\}|(\S+)")


def parse_critical_paths(output: str) -> list[tuple[str, float, list[str]]]:
    """The critical_paths_tcl() section: [(side, slack, nets)]."""
    _, sep, rest = output.partition(CRIT_BEGIN)
    body, end, _ = rest.partition(CRIT_END)
    if not sep or not end:
        raise PexError(f"no {CRIT_BEGIN} ... {CRIT_END} section (the "
                       "critical-path listing did not finish)")
    out = []
    for side, slack, words in _CRIT_RE.findall(body):
        try:
            s = float(slack)
        except ValueError:
            s = math.inf
        nets = [a or b for a, b in _TCL_WORD.findall(words)]
        out.append((side, s, nets))
    return out


def critical_nets(paths, n: int = CRIT_NETS) -> list[tuple[str, float, str]]:
    """The n nets on the worst paths as (net, slack, side): setup and hold
    ranked each against its own paths and taken in turn, so hold's small
    slacks do not crowd every setup path out. A net is at the worst slack
    of any path through it on its side, and named once."""
    per: dict[str, dict[str, float]] = {"max": {}, "min": {}}
    for side, slack, nets in paths:
        if not math.isfinite(slack) or side not in per:
            continue
        for net in nets:
            net = unescape(net)
            if slack < per[side].get(net, math.inf):
                per[side][net] = slack
    ranked = {side: sorted(d.items(), key=lambda kv: (kv[1], kv[0]))
              for side, d in per.items()}
    out: list[tuple[str, float, str]] = []
    seen: set[str] = set()
    cursor = {"max": 0, "min": 0}
    while len(out) < n and any(cursor[k] < len(ranked[k]) for k in cursor):
        for side in ("max", "min"):
            while cursor[side] < len(ranked[side]):
                net, slack = ranked[side][cursor[side]]
                cursor[side] += 1
                if net not in seen:
                    seen.add(net)
                    out.append((net, slack,
                                "setup" if side == "max" else "hold"))
                    break
            if len(out) >= n:
                break
    return out


def unescape(name: str) -> str:
    return name.replace("\\", "")


# ---------------------------------------------------------------- SPEF side

_UNIT_RE = re.compile(r"^\*([CR])_UNIT\s+([0-9.eE+-]+)\s+(\S+)", re.M)
_C_UNITS = {"FF": 1.0, "PF": 1e3, "NF": 1e6, "UF": 1e9}
_R_UNITS = {"OHM": 1.0, "KOHM": 1e3, "MOHM": 1e6}


def parse_spef(text: str) -> dict[str, dict]:
    """{net: {"c_ff": total C, "r_ohm": sum of *RES segments}} from a SPEF,
    names through its *NAME_MAP and with escapes dropped."""
    units = {"C": 1e3, "R": 1.0}   # the SPEF default would be PF / OHM
    for kind, mult, unit in _UNIT_RE.findall(text):
        table = _C_UNITS if kind == "C" else _R_UNITS
        if unit.upper() not in table:
            raise PexError(f"SPEF *{kind}_UNIT {unit!r} is not one pexlib "
                           "reads")
        units[kind] = float(mult) * table[unit.upper()]
    names: dict[str, str] = {}
    nets: dict[str, dict] = {}
    cur = None
    section = None
    for line in text.splitlines():
        t = line.split()
        if not t:
            continue
        head = t[0]
        if head == "*NAME_MAP":
            section = "map"
            continue
        if section == "map" and len(t) == 2 and re.fullmatch(r"\*\d+", head):
            names[head] = t[1]
            continue
        if head == "*D_NET" and len(t) >= 3:
            cur = unescape(names.get(t[1], t[1]))
            nets[cur] = {"c_ff": float(t[2]) * units["C"], "r_ohm": 0.0}
            section = None
            continue
        if head in ("*P", "*I", "*N"):
            continue                    # a *CONN entry or a node position
        if head.startswith("*") and not re.fullmatch(r"\*\d+", head):
            # *CONN, *CAP, *RES, *INDUC, *END, *PORTS, a header keyword:
            # only *RES lines are R, and any header closes the name map
            section = head
            if head == "*END":
                cur = None
            continue
        if section == "*RES" and cur and len(t) >= 4:
            nets[cur]["r_ohm"] += float(t[3]) * units["R"]
    if not nets:
        raise PexError("the SPEF has no *D_NET")
    return nets


# --------------------------------------------------------------- magic side

def magic_script(tech_lef: Path, lefs: list[Path], def_path: Path,
                 top: str, out_name: str = "flat") -> str:
    """magic Tcl that reads the routed DEF over the LEFs, flattens it (a
    hierarchical extraction leaves each cell's pin metal in the cell,
    off the net) and writes one ext2spice netlist with every capacitor."""
    lines = [f"lef read {_q(tech_lef)}"]
    lines += [f"lef read {_q(p)}" for p in lefs]
    lines += [
        f"def read {_q(def_path)} -noblockage -labels",
        f"load {_q(top)}",
        f"flatten {out_name}",
        f"load {out_name}",
        "select top cell",
        "extract do capacitance",
        "extract do coupling",
        "extract do resistance",
        "extract all",
        "ext2spice cthresh 0",
        "ext2spice rthresh 0",
        f"ext2spice -o {out_name}.spice",
        "quit -noprompt",
    ]
    return "\n".join(lines) + "\n"


def _q(p) -> str:
    s = str(p)
    if re.fullmatch(r"[\w@%+=:,./-]+", s):
        return s
    return "{" + s + "}"


def _si(value: str) -> float:
    """A spice capacitance in fF ("1.2f", "0.3p", "45a", or bare farads)."""
    v = value.lower()
    if v and v[-1] in _SI:
        return float(v[:-1]) * _SI[v[-1]]
    return float(v) * 1e15


def parse_spice_caps(text: str) -> dict[str, float]:
    """{node: fF}: every C line's value added to both of its nodes."""
    caps: dict[str, float] = {}
    for line in text.splitlines():
        t = line.split()
        if len(t) >= 4 and t[0][:1] in "Cc" and not t[0].startswith("*"):
            try:
                v = _si(t[3])
            except ValueError:
                continue
            for node in (t[1], t[2]):
                caps[node] = caps.get(node, 0.0) + v
    return caps


def parse_ext_r(text: str) -> dict[str, float]:
    """{node: ohm}: each node's metal as squares, (P/2)^2/A per
    resistclass, at the class's sheet resistance (a wire's length over its
    width; the .ext scale cancels)."""
    classes = None
    out: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("resistclasses"):
            classes = [float(x) for x in line.split()[1:]]
        elif line.startswith("node "):
            if classes is None:
                raise PexError("magic's .ext has node lines before its "
                               "resistclasses line")
            t = shlex.split(line)
            vals = [float(x) for x in t[7:7 + 2 * len(classes)]]
            r = 0.0
            for i, rs in enumerate(classes):
                if 2 * i + 1 >= len(vals) or rs >= METAL_RS_MAX_MOHM:
                    continue
                a, p = vals[2 * i], vals[2 * i + 1]
                if a > 0:
                    r += rs / 1000.0 * (p / 2.0) ** 2 / a
            out[t[1]] = r
    return out


# ------------------------------------------------------------------ DEF vias

_LEF_BLOCK = re.compile(r"^\s*(LAYER|VIA)\s+(\S+)(.*?)^\s*END\s+\2\b",
                        re.M | re.S)


def lef_via_r(tech_lef_text: str) -> tuple[dict[str, float], dict[str, float]]:
    """(via name -> ohm, cut layer name -> ohm per cut) from a tech LEF."""
    vias: dict[str, float] = {}
    cuts: dict[str, float] = {}
    for kind, name, body in _LEF_BLOCK.findall(tech_lef_text):
        m = re.search(r"^\s*RESISTANCE\s+([0-9.eE+-]+)\s*;", body, re.M)
        if not m:
            continue
        if kind == "VIA":
            vias[name] = float(m.group(1))
        elif re.search(r"^\s*TYPE\s+CUT\s*;", body, re.M):
            cuts[name] = float(m.group(1))
    return vias, cuts


def def_via_r(def_text: str, vias: dict[str, float],
              cuts: dict[str, float]) -> dict[str, float]:
    """{net: ohm of its routed vias}: a tech-LEF via at its RESISTANCE, a
    DEF-generated via (VIAS section, VIARULE ... ROWCOL r c) at its cut
    layer's per-cut resistance over its r x c cuts in parallel."""
    known = dict(vias)
    vsec = re.search(r"^VIAS\b.*?^END VIAS", def_text, re.M | re.S)
    if vsec:
        for blk in vsec.group(0).split(";"):
            m = re.search(r"^\s*-\s+(\S+)", blk, re.M)
            if not m:
                continue
            layers = re.search(r"\+\s*LAYERS\s+(\S+)\s+(\S+)\s+(\S+)", blk)
            rowcol = re.search(r"\+\s*ROWCOL\s+(\d+)\s+(\d+)", blk)
            if layers and layers.group(2) in cuts:
                n = (int(rowcol.group(1)) * int(rowcol.group(2))
                     if rowcol else 1)
                known[m.group(1)] = cuts[layers.group(2)] / max(n, 1)
    nsec = re.search(r"^NETS\b.*?^END NETS", def_text, re.M | re.S)
    out: dict[str, float] = {}
    if not nsec or not known:
        return out
    word = re.compile(r"[\w\[\]$/.\\-]+")
    for blk in nsec.group(0).split(";"):
        m = re.search(r"^\s*-\s+(\S+)", blk, re.M)
        if not m:
            continue
        out[unescape(m.group(1))] = sum(
            known.get(w, 0.0) for w in word.findall(blk) if w in known)
    return out


# ---------------------------------------------------------------- comparison

def _ratio(a: float, b: float) -> float | None:
    return round(a / b, 4) if b > 0 else None


def _off(ratio, a: float, b: float, tol: float, floor: float) -> bool:
    """Past tolerance and floor both; a zero OpenRCX figure has no ratio,
    so only the floor judges it."""
    if abs(a - b) <= floor:
        return False
    return ratio is None or abs(ratio - 1) > tol


def compare(nets, spef: dict[str, dict],
            magic_c: dict[str, float], magic_r: dict[str, float],
            via_r: dict[str, float]) -> dict:
    """Per-net magic/OpenRCX ratios for the named nets ((net, slack) or
    (net, slack, side)) and what is out of tolerance. A net either
    extractor has no figure for is `unmatched`. magic keeps a DEF name's
    escapes (`cnt\\[3\\]`); OpenSTA and the SPEF side drop them, so
    magic's names are unescaped here."""
    magic_c = {unescape(k): v for k, v in magic_c.items()}
    magic_r = {unescape(k): v for k, v in magic_r.items()}
    rows, unmatched = [], []
    for name, slack, *side in nets:
        s = spef.get(name)
        if s is None or name not in magic_c:
            unmatched.append(name)
            continue
        mc = magic_c[name]
        mr = magic_r.get(name, 0.0) + via_r.get(name, 0.0)
        row = {"net": name, "slack_ns": round(slack, 4),
               "rcx_c_ff": round(s["c_ff"], 4), "magic_c_ff": round(mc, 4),
               "c_ratio": _ratio(mc, s["c_ff"]),
               "rcx_r_ohm": round(s["r_ohm"], 3), "magic_r_ohm": round(mr, 3),
               "r_ratio": _ratio(mr, s["r_ohm"])}
        if side:
            row["side"] = side[0]
        row["c_mismatch"] = _off(row["c_ratio"], mc, s["c_ff"], C_TOL,
                                 C_FLOOR_FF)
        row["r_mismatch"] = _off(row["r_ratio"], mr, s["r_ohm"], R_TOL,
                                 R_FLOOR_OHM)
        rows.append(row)
    return {"nets": rows, "unmatched": unmatched,
            "c": _summary(rows, "c_ratio"), "r": _summary(rows, "r_ratio"),
            "thresholds": {"c_tol": C_TOL, "r_tol": R_TOL,
                           "c_floor_ff": C_FLOOR_FF,
                           "r_floor_ohm": R_FLOOR_OHM}}


def _summary(rows, key) -> dict:
    vals = [(r[key], r["net"]) for r in rows if r[key] is not None]
    if not vals:
        return {"median": None, "worst": None, "worst_net": None}
    worst = max(vals, key=lambda v: abs(math.log(v[0])) if v[0] > 0
                else math.inf)
    return {"median": round(statistics.median(v for v, _ in vals), 4),
            "worst": worst[0], "worst_net": worst[1]}


# ----------------------------------------------------------------------- run

def extract(eda_bin: Path, run_tool, tech_lef: Path, lefs: list[Path],
            def_path: Path, top: str, work_dir: Path, wall: float):
    """Run magic on the DEF in work_dir: (C by node, lumped R by node).
    run_tool is checklib.run_tool (a test fakes it)."""
    for label, p in [("tech LEF", tech_lef), ("DEF", def_path)] + [
            ("cell LEF", p) for p in lefs]:
        if not Path(p).is_file():
            raise PexError(f"no {label} at {p}")
    work_dir.mkdir(parents=True, exist_ok=True)
    for stale in ("flat.spice", "flat.ext"):
        (work_dir / stale).unlink(missing_ok=True)
    script = work_dir / "xcheck.tcl"
    script.write_text(magic_script(tech_lef, lefs, def_path, top),
                      encoding="utf-8")
    try:
        proc = run_tool([str(eda_bin), "magic", "-noconsole", "-dnull",
                         str(script)], wall=wall, cwd=str(work_dir),
                        stdin=subprocess.DEVNULL, capture_output=True,
                        text=True, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired as exc:
        raise PexError(f"eda magic timed out or stalled: {exc}") from exc
    log = (proc.stdout or "") + "\n" + (proc.stderr or "")
    (work_dir / "magic.log").write_text(log, encoding="utf-8")
    spice, ext = work_dir / "flat.spice", work_dir / "flat.ext"
    if not spice.is_file() or not ext.is_file():
        raise PexError(f"eda magic (exit {proc.returncode}) wrote no "
                       f"flat.spice/flat.ext: {log[-1500:]}")
    caps = parse_spice_caps(spice.read_text(encoding="utf-8",
                                            errors="replace"))
    res = parse_ext_r(ext.read_text(encoding="utf-8", errors="replace"))
    if not caps:
        raise PexError(f"magic's netlist {spice} has no capacitor - "
                       "the extraction did not run with capacitance on")
    return caps, res
