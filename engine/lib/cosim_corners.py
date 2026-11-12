"""cosim_corners.py - the pure half of the cosim gate's corner runs.

Which corners a mixed-signal top re-runs at, and which SDF goes with each:
no workspace writes, no simulator. engine/scripts/check_cosim.py runs what
this picks. Sign-off for a mixed-signal top is the top-level regression on
the post-layout views at the corners that bound the analog side, so the
default list comes from the analog side's own recorded post-layout sweep.
"""
from __future__ import annotations

import json
from pathlib import Path

from checklib import CheckError

# The digital view of a corner run: the RTL, or the digital side's hardened
# netlist back-annotated with the SDF for that corner.
VIEWS = ("rtl", "gl")
ENDS = {"min": min, "max": max}
CONFIG_KEYS = {"bound_by", "list", "digital", "env", "timeout_s", "jobs"}
DEFAULT_JOBS = 4
# One corner of a transistor-level top took about an hour on the PLL.
DEFAULT_TIMEOUT_S = 7200.0

BENCH_REL = "tb/cosim_bench.json"

# The evidence a bench writes beside its measures (cosimlib.corner_evidence).
CORNER_KEY = "cosim_corner"
NETLIST_KEY = "cosim_netlist"

# Where each record keeps its {corner: {measure: value}} table, in order of
# preference when two records cover the same number of corners.
PEX_RECORDS = (("reports/recorded/gate-pex_sim.json", "measured_by_corner",
                "log/pex_sim"),
               ("reports/recorded/pex_pvt.json", "measured", "log/pex_pvt"))

# The Liberty corner for a SPICE process: the cell library has no mixed
# sf/fs corners, so those take typical. Each process keeps one supply, its
# worst for setup or hold: ss at low supply, ff at high.
LIB_PROCESS = {"typical": "tt", "tt": "tt", "ss": "ss", "ff": "ff",
               "sf": "tt", "fs": "tt"}
SDF_BY_TEMP = {"ff": {-40: "nom_ff_n40C_3v60", 125: "nom_ff_125C_3v60"},
               "ss": {-40: "nom_ss_n40C_3v00", 125: "nom_ss_125C_3v00"},
               "tt": {25: "nom_tt_025C_3v30"}}

GEN_SDF_TCL = """\
read_liberty {lib}
read_verilog {netlist}
link_design {top}
read_spef {spef}
read_sdc {sdc}
write_sdf -include_typ -divider . {out}
"""


def validate_config(raw, where) -> dict | None:
    """`corners` from cosim_bench.json, with its defaults filled in; None
    when the bench does not declare one."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise CheckError(f"{where}: 'corners' must be a mapping")
    unknown = sorted(set(raw) - CONFIG_KEYS)
    if unknown:
        raise CheckError(f"{where}: 'corners' has unknown keys {unknown}; "
                         f"it takes {sorted(CONFIG_KEYS)}")
    bound_by, names = raw.get("bound_by"), raw.get("list")
    if bound_by is not None and names is not None:
        raise CheckError(f"{where}: 'corners' takes 'bound_by' or 'list', "
                         "not both")
    if bound_by is not None:
        if (not isinstance(bound_by, list) or not bound_by
                or not all(isinstance(p, list) and len(p) == 2
                           and isinstance(p[0], str) and p[1] in ENDS
                           for p in bound_by)):
            raise CheckError(f"{where}: 'corners.bound_by' must be a "
                             'non-empty list of [measure, "min"|"max"]')
    if names is not None and (
            not isinstance(names, list) or not names
            or not all(isinstance(n, str) and n for n in names)
            or len(set(names)) != len(names)):
        raise CheckError(f"{where}: 'corners.list' must be a non-empty list "
                         "of distinct corner names")
    digital = raw.get("digital")
    if digital is not None and (
            not isinstance(digital, list) or not digital
            or not all(v in VIEWS for v in digital)
            or len(set(digital)) != len(digital)):
        raise CheckError(f"{where}: 'corners.digital' must be a non-empty "
                         f"list of distinct views from {VIEWS}")
    env = raw.get("env", {})
    if not isinstance(env, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
        raise CheckError(f"{where}: 'corners.env' must map names to strings")
    timeout = raw.get("timeout_s", DEFAULT_TIMEOUT_S)
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or timeout <= 0):
        raise CheckError(f"{where}: 'corners.timeout_s' must be a positive "
                         "number")
    jobs = raw.get("jobs", DEFAULT_JOBS)
    if isinstance(jobs, bool) or not isinstance(jobs, int) or jobs <= 0:
        raise CheckError(f"{where}: 'corners.jobs' must be a positive integer")
    return {"bound_by": bound_by, "list": names, "digital": digital,
            "env": env, "timeout_s": float(timeout), "jobs": jobs}


def _table(path: Path, key: str) -> dict | None:
    """The {corner: {measure: value}} table a record holds, at the top level
    or under `facts`; None when the file or the table is not there."""
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CheckError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CheckError(f"{path} is not a JSON mapping")
    facts = data.get("facts") if isinstance(data.get("facts"), dict) else {}
    table = facts.get(key, data.get(key))
    if not table:
        return None
    if not isinstance(table, dict) or not all(
            isinstance(row, dict) for row in table.values()):
        raise CheckError(f"{path}: '{key}' must map each corner to its "
                         "measures")
    return table


def pex_record(analog: Path) -> dict | None:
    """The analog side's recorded post-layout sweep with the most corners,
    then the most measures (the gate's record keeps only what its spec
    bounds; a full tie goes to it), as {path, measured, log_dir}; None when
    it has neither."""
    best = None
    best_size = (0, 0)
    for rel, key, log_dir in PEX_RECORDS:
        table = _table(analog / rel, key)
        if not table:
            continue
        size = (len(table), len({m for row in table.values() for m in row}))
        if size > best_size:
            best, best_size = {"path": rel, "measured": table,
                               "log_dir": log_dir}, size
    return best


def not_owed_reason(ws: Path, analog_kind) -> str | None:
    """Why a bench that declares no `corners` owes no corner runs, None when
    it owes them. Reads files only, so the freshness map can ask it too."""
    import boundary  # pulls in the macro and characterisation libraries
    analog = ws / "analog"
    if analog_kind != "transistor":
        return ("the bench's analog side is an ideal model, with no "
                "post-layout view to run")
    if boundary.is_v1_block(ws):
        return "a released v1 block whose bench declares no corners"
    if not (analog / "spec" / "spec.yaml").is_file():
        return "no analog/spec/spec.yaml"
    if pex_record(analog) is None:
        return "the analog side has no recorded pex sweep"
    return None


def owed(ws: Path) -> bool:
    """Whether this workspace's cosim runs corners: the post-layout views
    are inputs of its cosim only then. A bench the gate would refuse answers
    by what it says, as far as it says anything. A broken pex record counts
    as owed, so its views stay hashed."""
    try:
        data = json.loads((ws / BENCH_REL).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if not isinstance(data, dict):
        return False
    if data.get("corners") is not None:
        return True
    try:
        return not_owed_reason(ws, data.get("analog_kind")) is None
    except CheckError:
        return True


def _number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def bounding_corners(measured: dict, bound_by=None) -> dict[str, list[str]]:
    """{corner: [why it bounds]}: the corner at each named end of each
    named measure, or, with no bound_by, at both ends of every numeric
    measure the record holds. A corner that bounds several ends runs once."""
    if bound_by is None:
        names = sorted({m for row in measured.values() for m, v in row.items()
                        if _number(v)})
        bound_by = [[m, end] for m in names for end in ("min", "max")]
    chosen: dict[str, list[str]] = {}
    for measure, end in bound_by:
        rows = {c: row[measure] for c, row in measured.items()
                if _number(row.get(measure))}
        if not rows:
            raise CheckError(f"corners.bound_by names {measure!r}, which no "
                             "corner of the recorded pex sweep measured")
        corner = ENDS[end](rows, key=rows.get)
        chosen.setdefault(corner, []).append(
            f"{end} {measure} {rows[corner]:.4g}")
    if not chosen:
        raise CheckError("the recorded pex sweep holds no numeric measure "
                         "to pick bounding corners from")
    return chosen


def sdf_corner_for(corner: dict) -> str:
    """The SDF corner for a sweep corner: its process, at the nearest
    temperature that process has (a tie takes the colder one)."""
    process = LIB_PROCESS.get(str(corner["process"]))
    if process is None:
        raise CheckError(f"corner {corner.get('name')!r}: no Liberty corner "
                         f"for process {corner['process']!r}")
    temps = SDF_BY_TEMP[process]
    temp = corner["temp_c"]
    return temps[min(temps, key=lambda t: (abs(t - temp), t))]


def sdf_file(final_dir: Path, dut: str, sdf_corner: str) -> Path | None:
    """The harden's SDF for this corner, or a `.gen.sdf` written beside it
    for a corner the harden does not produce; None when neither is there."""
    folder = final_dir / "sdf" / sdf_corner
    for name in (f"{dut}__{sdf_corner}.sdf", f"{dut}__{sdf_corner}.gen.sdf"):
        if (folder / name).is_file():
            return folder / name
    return None


def corner_not_applied(measures: dict | None, setup: dict) -> list[str]:
    """Why these measures do not show the bench ran `setup`'s corner and
    netlist (empty = they do). Without this a bench that ignores COSIM_SETUP
    would score its tt schematic run at every corner and pass them all."""
    if measures is None:
        return []  # evaluate_measures already says measures_missing
    out = []
    for key, want in ((CORNER_KEY, setup["corner"]),
                      (NETLIST_KEY, setup["netlist"])):
        if measures.get(key) != want:
            out.append(f"{key} is {measures.get(key)!r}, not {want!r}")
    return out
