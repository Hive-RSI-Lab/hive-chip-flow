"""qorlib - one fixed QoR table per harden, and the diff of two (#65).

LibreLane's `final/metrics.json` carries ~300 keys; harden used to keep five
families of them and drop every per-corner key. `extract` reads a fixed
schema out of it - area/utilisation, cell counts, worst and per-corner
setup/hold slack/TNS/violations, clock skew, power, DRC/antenna/DRV counts,
wirelength/vias, IR drop - and a metric the run did not write is listed in
`missing` and stays None: never read as 0.

`record` adds what a run needs to be replayed: the tool versions it ran on
(LibreLane from resolved.json's meta, yosys from the synthesis log,
OpenROAD from the tree; the PDK from statelib.pdk_version) and the sha256
of the generated config.json and of resolved.json.

`diff` compares two records against THRESHOLDS and returns regressions,
metrics the new run lost, metrics it could not compare, and version changes.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path

# name -> (metrics.json key, unit, better): better is "higher", "lower",
# "abs" (smaller magnitude: clock skew is signed, worst hold skew negative),
# "zero" (a count that should stay 0: any rise is a regression) or "info"
# (reported, never a regression: a repair buffer more or less is not worse
# in itself; what it costs shows in slack, area and power).
SCALARS: dict[str, tuple[str, str, str]] = {
    "die_area": ("design__die__area", "um^2", "lower"),
    "core_area": ("design__core__area", "um^2", "lower"),
    "stdcell_area": ("design__instance__area__stdcell", "um^2", "lower"),
    "utilization": ("design__instance__utilization", "frac", "lower"),
    "instance_count": ("design__instance__count", "", "lower"),
    "stdcell_count": ("design__instance__count__stdcell", "", "lower"),
    "setup_buffer_count": ("design__instance__count__setup_buffer", "", "info"),
    "hold_buffer_count": ("design__instance__count__hold_buffer", "", "info"),
    "setup_ws": ("timing__setup__ws", "ns", "higher"),
    "setup_tns": ("timing__setup__tns", "ns", "higher"),
    "setup_vio_count": ("timing__setup_vio__count", "", "zero"),
    "hold_ws": ("timing__hold__ws", "ns", "higher"),
    "hold_tns": ("timing__hold__tns", "ns", "higher"),
    "hold_vio_count": ("timing__hold_vio__count", "", "zero"),
    "skew_setup": ("clock__skew__worst_setup", "ns", "abs"),
    "skew_hold": ("clock__skew__worst_hold", "ns", "abs"),
    "power_total": ("power__total", "W", "lower"),
    "power_leakage": ("power__leakage__total", "W", "lower"),
    "power_switching": ("power__switching__total", "W", "lower"),
    "power_internal": ("power__internal__total", "W", "lower"),
    "route_drc_errors": ("route__drc_errors", "", "zero"),
    "magic_drc_errors": ("magic__drc_error__count", "", "zero"),
    "lvs_errors": ("design__lvs_error__count", "", "zero"),
    "antenna_nets": ("antenna__violating__nets", "", "zero"),
    "antenna_pins": ("antenna__violating__pins", "", "zero"),
    "route_antenna_violations": ("route__antenna_violation__count", "", "zero"),
    "max_slew_violations": ("design__max_slew_violation__count", "", "zero"),
    "max_cap_violations": ("design__max_cap_violation__count", "", "zero"),
    "max_fanout_violations": ("design__max_fanout_violation__count", "", "zero"),
    "floating_nets": ("timing__drv__floating__nets", "", "zero"),
    "wirelength": ("route__wirelength", "um", "lower"),
    "grt_wirelength": ("global_route__wirelength", "um", "lower"),
    "vias": ("route__vias", "", "lower"),
    "ir_drop_worst": ("ir__drop__worst", "V", "lower"),
}

# per corner: name -> (key prefix before `__corner:<c>`, unit, better)
PER_CORNER: dict[str, tuple[str, str, str]] = {
    "setup_ws": ("timing__setup__ws", "ns", "higher"),
    "setup_tns": ("timing__setup__tns", "ns", "higher"),
    "setup_vio_count": ("timing__setup_vio__count", "", "zero"),
    "hold_ws": ("timing__hold__ws", "ns", "higher"),
    "hold_tns": ("timing__hold__tns", "ns", "higher"),
    "hold_vio_count": ("timing__hold_vio__count", "", "zero"),
    "skew_setup": ("clock__skew__worst_setup", "ns", "abs"),
    "skew_hold": ("clock__skew__worst_hold", "ns", "abs"),
    "max_slew_violations": ("design__max_slew_violation__count", "", "zero"),
    "max_cap_violations": ("design__max_cap_violation__count", "", "zero"),
}

# How much worse counts as a regression: ns (absolute) for slack/TNS/skew,
# a fraction (relative) for area/power/wirelength, 0 for "zero" counts.
THRESHOLDS: dict[str, float] = {"ns_abs": 0.05, "rel": 0.02, "count_abs": 0}

VERSION_KEYS = ("librelane", "yosys", "openroad")


def _num(v):
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, (int, float)):
        return v
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def extract(metrics: dict) -> dict:
    """{metrics: {name: value|None}, corners: {corner: {name: value|None}},
    missing: [names]}; a corner-level name is `<corner>.<name>`."""
    out = {name: _num(metrics.get(key)) for name, (key, _, _) in SCALARS.items()}
    missing = [n for n, v in out.items() if v is None]
    corners = sorted({k.split("__corner:", 1)[1] for k in metrics
                      if "__corner:" in k and ":" not in k.split("__corner:", 1)[1]})
    per = {}
    for c in corners:
        row = {name: _num(metrics.get(f"{pfx}__corner:{c}"))
               for name, (pfx, _, _) in PER_CORNER.items()}
        missing += [f"{c}.{n}" for n, v in row.items() if v is None]
        per[c] = row
    return {"metrics": out, "corners": per, "missing": missing}


def _sha256_file(p: Path, drop_meta: bool = False) -> str | None:
    try:
        data = p.read_bytes()
    except OSError:
        return None
    try:
        obj = json.loads(data)
        if drop_meta and isinstance(obj, dict):
            obj = {k: v for k, v in obj.items() if k != "meta"}
        data = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    except ValueError:
        pass
    return hashlib.sha256(data).hexdigest()


_YOSYS_RE = re.compile(r"Yosys \d[^\s,()]*")


def run_versions(run_dir: Path) -> dict:
    """What the run itself says it ran on: LibreLane from resolved.json's
    meta, yosys from the synthesis step's log. None where absent."""
    out = {"librelane": None, "yosys": None}
    try:
        meta = json.loads((run_dir / "resolved.json").read_text(
            encoding="utf-8")).get("meta") or {}
        out["librelane"] = meta.get("librelane_version")
    except (OSError, ValueError, AttributeError):
        pass
    for log in sorted(run_dir.glob("*-yosys-synthesis/*.log")):
        try:
            m = _YOSYS_RE.search(log.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
        if m:
            out["yosys"] = m.group(0).split()[1]
            break
    return out


def env_versions(eda_bin: Path, timeout: float = 60.0) -> dict:
    """The tree's librelane/yosys/openroad versions, through bin/eda. None
    for a tool that does not answer (never a guess)."""
    def ask(args, rx):
        try:
            proc = subprocess.run([str(eda_bin), *args], capture_output=True,
                                  text=True, encoding="utf-8",
                                  errors="replace", timeout=timeout, input="")
        except (OSError, subprocess.TimeoutExpired):
            return None
        if proc.returncode != 0:
            return None
        m = re.search(rx, (proc.stdout or "") + (proc.stderr or ""))
        return m.group(1) if m else None
    return {
        "librelane": ask(["python", "-c",
                          "import librelane; print(librelane.__version__)"],
                         r"^\s*(\S+)"),
        "yosys": ask(["yosys", "--version"], r"Yosys (\d[^\s,()]*)"),
        "openroad": ask(["openroad", "-version"], r"^\s*(\S+)"),
    }


def record(metrics: dict, run_dir: Path, config_path: Path | None,
           env: dict | None = None, pdk: dict | None = None) -> dict:
    """The QoR table plus versions and config hashes for one run. `env` is
    env_versions() (the tree the run was launched from): a version the run
    itself names wins over it."""
    rec = extract(metrics)
    versions = {k: None for k in VERSION_KEYS}
    versions.update({k: v for k, v in (env or {}).items() if k in versions})
    versions.update({k: v for k, v in run_versions(run_dir).items() if v})
    if pdk is not None:
        versions["pdk"] = {k: v for k, v in pdk.items() if k != "root"}
    rec["versions"] = versions
    rec["config_sha256"] = (_sha256_file(config_path)
                            if config_path is not None else None)
    # meta carries the LibreLane version, already recorded above
    rec["resolved_sha256"] = _sha256_file(run_dir / "resolved.json",
                                          drop_meta=True)
    return rec


def _rows(rec: dict):
    """(name, value, unit, better) for every scalar and corner metric."""
    for name, (_, unit, better) in SCALARS.items():
        yield name, rec.get("metrics", {}).get(name), unit, better
    for c, row in sorted((rec.get("corners") or {}).items()):
        for name, (_, unit, better) in PER_CORNER.items():
            yield f"{c}.{name}", row.get(name), unit, better


def _worse(old: float, new: float, unit: str, better: str,
           th: dict) -> bool:
    if better == "info":
        return False
    if better == "zero":
        return new > old + th["count_abs"]
    if better == "abs":
        old, new = abs(old), abs(new)
    delta = (old - new) if better == "higher" else (new - old)
    if delta <= 0:
        return False
    if unit == "ns":
        return delta > th["ns_abs"]
    return delta > abs(old) * th["rel"]     # from 0: any rise


def diff(old: dict, new: dict, thresholds: dict | None = None) -> dict:
    """{regressions, lost, not_compared, deltas, versions_changed,
    config_changed}: regressions are metrics worse than the threshold; lost
    are metrics the old run had and the new one does not; not_compared are
    metrics the old run lacks, so nothing says the new value is no worse. A
    metric that could not be compared never reads as clean."""
    th = dict(THRESHOLDS)
    th.update(thresholds or {})
    old_rows = {n: v for n, v, _, _ in _rows(old)}
    regressions, lost, not_compared, deltas = [], [], [], []
    for name, nv, unit, better in _rows(new):
        ov = old_rows.get(name)
        if ov is None:
            not_compared.append(name)
            continue
        if nv is None:
            lost.append(name)
            continue
        if nv != ov:
            deltas.append({"metric": name, "old": ov, "new": nv, "unit": unit})
        if _worse(ov, nv, unit, better, th):
            regressions.append({"metric": name, "old": ov, "new": nv,
                                "unit": unit, "better": better})
    # a corner the old run had and the new one dropped entirely
    for c in sorted(set(old.get("corners") or {}) - set(new.get("corners") or {})):
        lost += [f"{c}.{n}" for n, v in old["corners"][c].items() if v is not None]
    ov_, nv_ = old.get("versions") or {}, new.get("versions") or {}
    versions_changed = {k: {"old": ov_.get(k), "new": nv_.get(k)}
                        for k in sorted(set(ov_) | set(nv_))
                        if ov_.get(k) != nv_.get(k)}
    return {"regressions": regressions, "lost": sorted(lost),
            "not_compared": sorted(not_compared), "deltas": deltas,
            "versions_changed": versions_changed,
            "config_changed": old.get("config_sha256") != new.get("config_sha256"),
            "thresholds": th}


def reproducible(rec: dict, env: dict, pdk: dict | None = None) -> list[str]:
    """Why `rec` cannot be replayed on this environment: one line per tool
    (or the PDK) whose recorded version differs or was never recorded."""
    why = []
    versions = rec.get("versions") or {}
    for k in VERSION_KEYS:
        rv, ev = versions.get(k), env.get(k)
        if rv is None:
            why.append(f"{k}: no version recorded")
        elif ev is None:
            why.append(f"{k}: recorded {rv}, this environment does not answer")
        elif rv != ev:
            why.append(f"{k}: recorded {rv}, this environment has {ev}")
    if pdk is not None:
        cur = {k: v for k, v in pdk.items() if k != "root"}
        if versions.get("pdk") != cur:
            why.append(f"pdk: recorded {versions.get('pdk')}, this "
                       f"environment has {cur}")
    return why


def table(rec: dict) -> str:
    """Plain-text table of a record (design doc, `qor show --text`)."""
    lines = [f"{'metric':<40} {'value':>14} unit"]
    for name, v, unit, _ in _rows(rec):
        val = "MISSING" if v is None else f"{v:.6g}"
        lines.append(f"{name:<40} {val:>14} {unit}")
    vs = rec.get("versions") or {}
    lines.append("versions: " + ", ".join(f"{k}={vs.get(k)}" for k in vs))
    lines.append(f"config_sha256: {rec.get('config_sha256')}")
    return "\n".join(lines)
