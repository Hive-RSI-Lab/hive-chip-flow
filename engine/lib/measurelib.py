"""measurelib.py - a vde `check: measure` requirement, checked (issue #89).

A digital requirement with `check: measure` used to need only `bounds` to
pass spec_lint, and no vde gate ever read them: the requirement passed every
gate whether it was met or not. Now each bound names the recorded number it
is held to, and release (check_release.py) reads that number and fails a
bound it misses or a number nobody recorded.

Shape, in spec.yaml (speclib.lint_spec refuses anything else):

    - id: REQ-TIM-REF
      check: measure
      bounds:
        clk_wns_ns: {min: 0, source: timing.corners.max_tt_025C_3v30.setup_ws}
        lvs_matched: {equals: true, source: lvs.matched}

`source` is `<gate>.<path>`: <gate> a vde gate in gates.yaml, <path> dotted
keys into that gate's recorded result's `facts`
(reports/recorded/gate-<gate>.json, which gate.py writes on every recorded
run). An `msde:` prefix (`msde:cosim.measures.lock_time_us`) reads the
enclosing msde workspace's record instead - a nested digital/ side whose
loop numbers only the msde cosim measures. Every bound has at least one of
min / max (numbers) or equals (a number, bool or string), and may carry a
`note` saying how far the number covers the requirement; the note travels
with the row.

Each bound evaluates to one row: status pass, fail (a value outside its
limits), missing (no recorded run of that gate, or the path is not in its
facts, or the value is not a number a min/max can compare), or deferred.
Missing is never a pass.

Deferred is an `msde:` bound seen from its nested digital side: the msde flow
releases digital at P2 and runs cosim at P3, so digital's release cannot hold
a number cosim has yet to record. The msde release checks those bounds
instead (nested_msde_rows), where attest has already required a fresh passing
cosim. A block with no enclosing msde workspace has nobody to defer to, so
there the bound reads missing.

These rows read the records as they stand. Release is what also requires
them fresh (attest); measures.py on its own does not.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

import yaml

ENGINE = Path(__file__).resolve().parent.parent
GATES_YAML = ENGINE / "reference" / "gates.yaml"
RECORDED_DIR = Path("reports") / "recorded"
SOURCE_RE = re.compile(r"^(?:(msde):)?([a-z][a-z0-9_]*)((?:\.[^.\s]+)+)$")
LIMIT_KEYS = ("min", "max", "equals")
BOUND_KEYS = set(LIMIT_KEYS) | {"source", "note"}


def gate_names(skill: str) -> set[str]:
    data = yaml.safe_load(GATES_YAML.read_text(encoding="utf-8")) or {}
    return set((data.get("gates") or {}).get(skill) or {})


def parse_source(source) -> tuple[str, str, list[str]] | None:
    """(skill, gate, path keys) for a well-formed source, else None."""
    if not isinstance(source, str):
        return None
    m = SOURCE_RE.match(source.strip())
    if not m:
        return None
    return (m.group(1) or "vde", m.group(2), m.group(3)[1:].split("."))


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) \
        and math.isfinite(v)


def lint_bound(name, bound, known: dict[str, set[str]]) -> list[tuple[str, str]]:
    """(kind, message) problems with one bound of a vde measure
    requirement; [] when it is checkable."""
    if not isinstance(bound, dict):
        return [("requirement_measure_bad_bound",
                 f"bound {name!r} is not a mapping {{min?, max?, equals?, "
                 "source}}")]
    out = []
    extra = sorted(set(bound) - BOUND_KEYS)
    if extra:
        out.append(("requirement_measure_bad_bound",
                    f"bound {name!r} has unknown key(s) {extra}"))
    limits = [k for k in LIMIT_KEYS if k in bound]
    if not limits:
        out.append(("requirement_measure_bad_bound",
                    f"bound {name!r} has no limit (min, max or equals)"))
    for k in ("min", "max"):
        if k in bound and not _is_num(bound[k]):
            out.append(("requirement_measure_bad_bound",
                        f"bound {name!r} {k} must be a number, got "
                        f"{bound[k]!r}"))
    if "equals" in bound and ("min" in bound or "max" in bound):
        out.append(("requirement_measure_bad_bound",
                    f"bound {name!r} mixes equals with min/max"))
    src = bound.get("source")
    if src is None or (isinstance(src, str) and not src.strip()):
        out.append(("requirement_measure_no_source",
                    f"bound {name!r} names no source - say which recorded "
                    "number it is held to, as <gate>.<facts path> (e.g. "
                    "timing.corners.max_tt_025C_3v30.setup_ws), or the "
                    "requirement is checked by nothing"))
        return out
    parsed = parse_source(src)
    if parsed is None:
        out.append(("requirement_measure_bad_source",
                    f"bound {name!r} source {src!r} is not "
                    "[msde:]<gate>.<facts path>"))
        return out
    skill, gate, _ = parsed
    if gate not in known.get(skill, set()):
        out.append(("requirement_measure_bad_source",
                    f"bound {name!r} source {src!r}: {gate!r} is not a "
                    f"{skill} gate in gates.yaml"))
    return out


def measure_requirements(spec: dict) -> list[dict]:
    return [r for r in (spec.get("requirements") or [])
            if isinstance(r, dict) and r.get("check") == "measure"]


def _record_dir(ws: Path, skill: str) -> Path | None:
    if skill == "vde":
        return ws
    parent = ws.resolve().parent
    try:
        st = json.loads((parent / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return parent if st.get("skill") == skill else None


def resolve(ws: Path, source: str, cache: dict | None = None):
    """(value, None) or (None, why it is missing)."""
    parsed = parse_source(source)
    if parsed is None:
        return None, f"source {source!r} is malformed"
    skill, gate, keys = parsed
    root = _record_dir(ws, skill)
    if root is None:
        return None, (f"no enclosing {skill} workspace (state.json skill "
                      f"{skill!r}) above {ws}")
    path = root / RECORDED_DIR / f"gate-{gate}.json"
    cache = {} if cache is None else cache
    if path not in cache:
        try:
            cache[path] = json.loads(path.read_text(encoding="utf-8"))
        except OSError:
            cache[path] = None
        except ValueError:
            cache[path] = {"_bad": True}
    rec = cache[path]
    where = f"{skill}:{gate}" if skill != "vde" else gate
    if rec is None:
        return None, f"no recorded {where} run ({RECORDED_DIR}/{path.name})"
    if rec.get("_bad"):
        return None, f"{RECORDED_DIR}/{path.name} is not valid JSON"
    node = rec.get("facts") or {}
    for i, k in enumerate(keys):
        if not isinstance(node, dict) or k not in node:
            return None, (f"recorded {where} facts have no "
                          f"{'.'.join(keys[:i + 1])!r}")
        node = node[k]
    return node, None


def nested_msde_rows(ws: Path, spec: dict) -> list[dict]:
    """The `msde:` rows of nested digital spec `spec`, resolved rather than
    deferred - what the enclosing msde release owes (module docstring)."""
    return [r for r in evaluate(ws, spec, defer_msde=False)
            if (parse_source(r["source"]) or ("",))[0] == "msde"]


def evaluate(ws: Path, spec: dict, defer_msde: bool = True) -> list[dict]:
    """One row per bound of every measure requirement:
    {req, name, source, value, min?, max?, equals?, note?, status, why}."""
    rows, cache = [], {}
    for req in measure_requirements(spec):
        bounds = req.get("bounds") or {}
        if not isinstance(bounds, dict):
            bounds = {}
        for name, b in bounds.items():
            b = b if isinstance(b, dict) else {}
            row = {"req": req.get("id"), "name": name,
                   "source": b.get("source")}
            row.update({k: b[k] for k in (*LIMIT_KEYS, "note") if k in b})
            if not b.get("source"):
                rows.append({**row, "value": None, "status": "missing",
                             "why": "the bound names no source"})
                continue
            parsed = parse_source(b["source"])
            if defer_msde and parsed and parsed[0] == "msde" \
                    and _record_dir(ws, "msde") is not None:
                rows.append({**row, "value": None, "status": "deferred",
                             "why": "checked by the enclosing msde release"})
                continue
            value, why = resolve(ws, b["source"], cache)
            row["value"] = value
            if why:
                rows.append({**row, "status": "missing", "why": why})
                continue
            if "equals" in b:
                eq = b["equals"]
                if _is_num(eq) and _is_num(value):
                    ok = value == eq
                else:
                    ok = type(value) is type(eq) and value == eq
                rows.append({**row, "status": "pass" if ok else "fail",
                             "why": "" if ok else
                             f"{value!r} != {b['equals']!r}"})
                continue
            if not _is_num(value):
                rows.append({**row, "status": "missing",
                             "why": f"recorded value {value!r} is not a "
                                    "number"})
                continue
            bad = []
            if "min" in b and value < b["min"]:
                bad.append(f"{value} < min {b['min']}")
            if "max" in b and value > b["max"]:
                bad.append(f"{value} > max {b['max']}")
            rows.append({**row, "status": "fail" if bad else "pass",
                         "why": "; ".join(bad)})
    return rows
