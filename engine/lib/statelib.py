"""statelib.py - normalized artifact hashing + freshness computation.

Ported from /hwde's scripts/lib/statelib.py (docs/design.md 1.3), domain
swapped: KiCad's s-expression/gerber normalizers are gone (no `sexpr_no_uuid`,
no `gerber_design`); a chip-flow block's artifacts are HDL/SPICE text, spec
YAML/JSON, and test/reference directories, so the normalizer set is
text_eol, json_canonical (spec.yaml, config.json - anything YAML-or-JSON
shaped) and dir_text (rtl/, tb/, holdout/, ...). `gds_geometry` (a hardened
GDS hashed through klayout's python module, timestamps stripped) lands with
the milestone that first produces a GDS (M4/M9) - not needed while every
gate is a stub.

state.py owns the schema and CLI; this module owns everything that turns
files into design-content hashes and hashes into freshness verdicts.
Toolchain-free by design (pure venv: yaml) so freshness questions never
need the eda image.

Why normalized hashes: raw file hashes are NOT design fingerprints - a
regenerated file can carry timestamp/whitespace churn that says "different
file" for content that means the same design. Each artifact KIND therefore
hashes through a normalizer that removes exactly the churn that does not
change the design, and nothing else. Stored hashes are prefixed "<norm>:" so
a normalizer change can never false-match a hash computed under the old
rules.

A gate's own code is not an input: hashing the engine would stale every block
on any refactor. A change that can turn a pass into a fail bumps the gate's
`strength` in reference/gates.yaml instead, and a record at a lower strength
reads stale (gate_freshness).

The artifact kinds live in reference/invalidation.yaml (single source,
shared across skills - the same block layout, docs/design.md 1.4). The
per-gate input sets and the edit-class invalidation map are SKILL-scoped
there (gate names collide across /vde, /ade, /msde with different meanings,
docs/design.md 1.5) - every lookup here takes a `skill`.

Failure direction is always conservative: an unparsable input falls back to a
raw-bytes hash (only an identical file matches), an unknown gate hashes no
inputs (freshness "unknown", never "fresh"), a missing file hashes to None
(match only if it was also missing at record time).
"""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

import yaml

# engine/lib/statelib.py -> parents[0]=engine/lib, parents[1]=engine
DEFAULT_MAP = Path(__file__).resolve().parents[1] / "reference" / "invalidation.yaml"
DEFAULT_GATES = Path(__file__).resolve().parents[1] / "reference" / "gates.yaml"
RC_VARIANTS_PATH = Path(__file__).resolve().parents[1] / "reference" / "pex_rc_variants.yaml"

_MAP_CACHE: dict[tuple[str, float], dict] = {}
_STRENGTH_CACHE: dict[tuple[str, float], dict] = {}

SKILLS = ("vde", "ade", "msde")


# ---------------------------------------------------------------------------
# invalidation map
# ---------------------------------------------------------------------------
def load_map(path: Path | str | None = None) -> dict:
    """Parsed invalidation.yaml, validated. Cached on (path, mtime).

    Shape: {artifact_kinds: {kind: {path, norm}}, gate_inputs: {skill:
    {gate: [kind...]}}, edit_classes: {skill: {class: {mutates,
    stale_artifacts, gates, human_hold}}}}. artifact_kinds is FLAT - the
    same block layout (docs/design.md 1.4) is shared by every skill;
    gate_inputs and edit_classes are per-skill because gate names collide
    across skills with different meanings (drc, lvs, release, spec_lint)."""
    p = Path(path) if path else DEFAULT_MAP
    key = (str(p), p.stat().st_mtime)
    if key in _MAP_CACHE:
        return _MAP_CACHE[key]
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    if not isinstance(data.get("artifact_kinds"), dict) or not data["artifact_kinds"]:
        raise ValueError(f"{p}: missing/empty section 'artifact_kinds'")
    for section in ("gate_inputs", "edit_classes"):
        if not isinstance(data.get(section), dict) or not data[section]:
            raise ValueError(f"{p}: missing/empty section {section!r}")
        unknown_skills = set(data[section]) - set(SKILLS)
        if unknown_skills:
            raise ValueError(f"{p}: {section} names unknown skills "
                             f"{sorted(unknown_skills)}")
    for skill in data["gate_inputs"]:
        for gate, kinds in data["gate_inputs"][skill].items():
            unknown = [k for k in kinds if k not in data["artifact_kinds"]]
            if unknown:
                raise ValueError(f"{p}: {skill}.gate_inputs[{gate!r}] names "
                                 f"unknown artifact kinds {unknown}")
    _check_inputs_when(p, data)
    writes = data.get("gate_writes") or {}
    if not isinstance(writes, dict):
        raise ValueError(f"{p}: gate_writes is not a mapping")
    unknown_skills = set(writes) - set(SKILLS)
    if unknown_skills:
        raise ValueError(f"{p}: gate_writes names unknown skills "
                         f"{sorted(unknown_skills)}")
    for skill, rows in writes.items():
        for gate, kinds in (rows or {}).items():
            if gate not in data["gate_inputs"].get(skill, {}):
                raise ValueError(f"{p}: {skill}.gate_writes names unknown "
                                 f"gate {gate!r}")
            unknown = [k for k in kinds if k not in data["artifact_kinds"]]
            if unknown:
                raise ValueError(f"{p}: {skill}.gate_writes[{gate!r}] names "
                                 f"unknown artifact kinds {unknown}")
        _check_gate_order(p, skill, data)
    for skill in data["edit_classes"]:
        gate_inputs = data["gate_inputs"].get(skill, {})
        for cls, ec in data["edit_classes"][skill].items():
            for field in ("mutates", "stale_artifacts", "gates", "human_hold"):
                if field not in ec:
                    raise ValueError(f"{p}: {skill}.{cls} lacks {field!r}")
            unknown = [k for k in ec["mutates"] + ec["stale_artifacts"]
                       if k not in data["artifact_kinds"]]
            if unknown:
                raise ValueError(f"{p}: {skill}.{cls} names unknown artifact "
                                 f"kinds {unknown}")
            unknown = [g for g in ec["gates"] if g not in gate_inputs]
            if unknown:
                raise ValueError(f"{p}: {skill}.{cls} names unknown gates "
                                 f"{unknown}")
    _MAP_CACHE[key] = data
    return data


def _check_inputs_when(p: Path, data: dict) -> None:
    """Raise when gate_inputs_when names a predicate this module lacks, or a
    kind outside the gate's own row: such a row could never apply, and the
    input would silently be hashed everywhere or nowhere."""
    for skill, gates in (data.get("gate_inputs_when") or {}).items():
        for gate, preds in (gates or {}).items():
            row = (data["gate_inputs"].get(skill) or {}).get(gate) or []
            for pred, kinds in (preds or {}).items():
                if pred not in INPUT_PREDICATES:
                    raise ValueError(f"{p}: gate_inputs_when names unknown "
                                     f"predicate {pred!r}")
                outside = [k for k in kinds if k not in row]
                if outside:
                    raise ValueError(f"{p}: gate_inputs_when[{skill}.{gate}] "
                                     f"names kinds not in its row: {outside}")


def _check_gate_order(p: Path, skill: str, data: dict) -> None:
    """Raise when gate_writes puts a skill's gates in a cycle: then no gate
    could ever read fresh after the other, and the order is not clean."""
    order = {g: upstream_gates(skill, g, data)
             for g in data["gate_inputs"].get(skill, {})}
    state: dict[str, int] = {}

    def visit(g: str, path: list[str]) -> None:
        if state.get(g) == 2:
            return
        if state.get(g) == 1:
            raise ValueError(f"{p}: {skill}.gate_writes puts gates in a "
                             f"cycle: {' -> '.join(path + [g])}")
        state[g] = 1
        for u in order.get(g, []):
            visit(u, path + [g])
        state[g] = 2

    for g in order:
        visit(g, [])


# ---------------------------------------------------------------------------
# normalizers (each returns the BYTES to hash; None = use raw file bytes)
# ---------------------------------------------------------------------------
def _text(data: bytes) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def _eol(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _norm_text_eol(path: Path) -> bytes:
    return _eol(_text(path.read_bytes())).encode("utf-8")


def _norm_json_canonical(path: Path) -> bytes:
    """Parsed structured content, re-serialized with sorted keys - JSON or
    YAML (spec.yaml, config.json: a chip-flow spec is written in YAML, a
    LibreLane config in JSON; both are 'specs and configs', docs/design.md
    1.6, and yaml.safe_load reads JSON as a YAML subset)."""
    doc = yaml.safe_load(_text(path.read_bytes()))
    return json.dumps(doc, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":")).encode("utf-8")


def _req_sans_source(req):
    """A requirement without the source/note of its measure bounds."""
    if not (isinstance(req, dict) and req.get("check") == "measure"
            and isinstance(req.get("bounds"), dict)):
        return req
    bounds = {n: ({k: v for k, v in b.items() if k not in ("source", "note")}
                  if isinstance(b, dict) else b)
              for n, b in req["bounds"].items()}
    return {**req, "bounds": bounds}


def _norm_json_canonical_sans_formal(path: Path) -> bytes:
    """json_canonical of spec.yaml without its top-level `formal:` key - the
    spec as every vde gate that never reads `formal:` sees it (only
    check_formal.py does), so a formal-depth-only edit leaves their recorded
    passes fresh while any other edit still changes this hash. A document
    that is not a mapping raises, so hash_artifact's raw fallback takes
    over (fail-safe: over-invalidates, never under)."""
    doc = yaml.safe_load(_text(path.read_bytes()))
    if not isinstance(doc, dict):
        raise ValueError(f"{path} is not a mapping")
    doc = {k: v for k, v in doc.items() if k != "formal"}
    # a measure bound's source/note say where release reads a number, and no
    # gate that hashes this reads them, so wiring a source into a hardened
    # block leaves its passes fresh (measurelib.py, #89). Limits still count.
    reqs = doc.get("requirements")
    if isinstance(reqs, list):
        doc["requirements"] = [_req_sans_source(r) for r in reqs]
    return json.dumps(doc, sort_keys=True, ensure_ascii=True,
                      separators=(",", ":")).encode("utf-8")


def _config_roots(doc: dict, path: Path) -> list[tuple[str, str]]:
    """The machine roots in a generated LibreLane config, longest first: the
    block's workspace and chip-flow's checkout as the config itself names
    them (the TT wrapper `<ws>/harden/tt_um_*.v` among VERILOG_FILES, a
    `<home>/engine/reference/...` template) and as they are on this machine,
    and the config's own PDK_ROOT. Read from the document, so a config
    written in another checkout or on another machine normalizes the same
    as one written here."""
    roots = [(str(path.resolve().parent.parent), "${BLOCK}"),
             (str(Path(__file__).resolve().parents[2]), "${CHIP_FLOW_HOME}")]
    strings = [v for v in doc.values() if isinstance(v, str)]
    for v in doc.values():
        if isinstance(v, list):
            strings += [x for x in v if isinstance(x, str)]
    for v in strings:
        m = re.fullmatch(r"(/.+)/harden/tt_um_[^/]+\.v", v)
        if m:
            roots.append((m.group(1), "${BLOCK}"))
        m = re.match(r"(/.+?)/engine/reference/", v)
        if m:
            roots.append((m.group(1), "${CHIP_FLOW_HOME}"))
    pdk_roots = [doc.get("PDK_ROOT")]
    # a config check_harden writes now names no PDK_ROOT (it goes on the
    # command line): the one its runs resolved stands in
    for resolved in sorted(path.parent.glob("runs/*/resolved.json")):
        try:
            pdk_roots.append(json.loads(resolved.read_text(
                encoding="utf-8")).get("PDK_ROOT"))
        except (OSError, ValueError, AttributeError):
            pass
    for pdk in pdk_roots:
        if isinstance(pdk, str) and pdk.startswith("/"):
            roots.append((pdk.rstrip("/"), "${PDK_ROOT}"))
    return sorted(set(roots), key=lambda r: -len(r[0]))


def _norm_json_canonical_sans_roots(path: Path) -> bytes:
    """json_canonical of a generated LibreLane config (harden/config.json)
    with the machine's own roots written as placeholders (_config_roots):
    the block's workspace, chip-flow's checkout and PDK_ROOT.
    check_harden.py rewrites the file on every run with absolute paths, so
    hashed as it is, a block hardened in another checkout, on another
    machine or from a scratch copy read its own config as an edit made
    during the run, and the gate went stale on its first pass. Any other
    change still moves the hash. A document that is not a mapping raises
    (hash_artifact's raw fallback)."""
    doc = yaml.safe_load(_text(path.read_bytes()))
    if not isinstance(doc, dict):
        raise ValueError(f"{path} is not a mapping")
    roots = _config_roots(doc, path)

    def sub(v):
        if isinstance(v, str):
            for root, name in roots:
                if v == root or v.startswith(root + "/"):
                    return name + v[len(root):]
            return v
        if isinstance(v, list):
            return [sub(x) for x in v]
        if isinstance(v, dict):
            return {k: sub(x) for k, x in v.items()}
        return v
    return json.dumps(sub(doc), sort_keys=True, ensure_ascii=True,
                      separators=(",", ":")).encode("utf-8")


def _gds_sans_timestamps(data: bytes) -> bytes:
    """A GDSII stream with the modification and access dates of its BGNLIB
    and BGNSTR records zeroed: the only bytes two exports of the same
    geometry differ in. Not a GDSII stream (a short or broken record) raises,
    so the caller can fall back to the raw bytes."""
    out = bytearray(data)
    i = 0
    while i + 4 <= len(out):
        n = int.from_bytes(out[i:i + 2], "big")
        if n == 0:  # padding after ENDLIB
            if any(out[i:]):
                raise ValueError("not a GDSII stream")
            break
        if n < 4 or i + n > len(out):
            raise ValueError("not a GDSII stream")
        if out[i + 2] in (0x01, 0x05) and n == 28:  # BGNLIB, BGNSTR
            out[i + 4:i + 28] = bytes(24)
        i += n
    return bytes(out)


# Lines a LibreLane export stamps with the time it ran: SDF's (DATE ...),
# SPEF's *DATE, magic's `timestamp N`.
_RUN_STAMP = re.compile(r"^\s*(\(DATE \".*\"\)|\*DATE \".*\"|timestamp \d+)\s*$")
# What in a harden run's final/ is not its signoff geometry and timing: the
# OpenROAD database (binary, carried by def/), the yosys JSON header (source
# paths) and the run's metrics (tool runtimes).
_HARDEN_SKIP = ("odb", "json_h", "metrics.csv", "metrics.json")


# The committed record of what a harden handed on, in its harden/ dir (#239).
# Never part of the hash it holds.
HARDEN_RECORD = "outputs.json"


def _norm_harden_signoff(path: Path, with_runs: bool = True) -> bytes:
    """A harden/ dir as the gates after harden read it: its own files (the
    generated config through json_canonical_sans_roots), and of its runs only
    each run's final/ outputs, with the time each export ran and the
    machine's roots (the block, chip-flow, the PDK) taken out. An unchanged
    block re-hardened in another checkout, on another machine or tomorrow
    reproduces the netlist, LEF and DEF byte for byte and the GDS, SDF, SPEF
    and magic views but for those stamps (r2r_dac, 2026-10-28), so its
    recorded results and the person's waivers over them hold; any change to
    the geometry, netlist, timing views or config moves the hash. Logs and
    the step directories LibreLane leaves are not inputs of anything.
    with_runs=False is the own files alone, which a harden record vouches
    for (harden_record_hash)."""
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    cfg = path / "config.json"
    try:
        doc = yaml.safe_load(_text(cfg.read_bytes())) if cfg.is_file() else {}
    except yaml.YAMLError:
        doc = {}
    roots = _config_roots(doc if isinstance(doc, dict) else {}, cfg)

    def text(f: Path) -> bytes:
        t = _eol(_text(f.read_bytes()))
        for root, name in roots:
            t = t.replace(root, name)
        return "\n".join(l for l in t.split("\n")
                         if not _RUN_STAMP.match(l)).encode("utf-8")

    files = [f for f in path.iterdir()
             if f.is_file() and f.name != HARDEN_RECORD]
    runs = path / "runs"
    if with_runs and runs.is_dir():
        for final in sorted(runs.glob("*/final")):
            files += [f for f in final.rglob("*") if f.is_file()
                      and not any(part in _HARDEN_SKIP
                                  for part in f.relative_to(final).parts)]
    h_parts: list[bytes] = []
    for f in sorted(files, key=lambda f: f.relative_to(path).as_posix()):
        rel = f.relative_to(path).as_posix()
        if f == cfg:
            try:
                body = _norm_json_canonical_sans_roots(f)
            except (ValueError, yaml.YAMLError):
                body = f.read_bytes()
        elif f.suffix == ".gds":
            try:
                body = _gds_sans_timestamps(f.read_bytes())
            except ValueError:
                body = f.read_bytes()
        else:
            body = text(f)
        h_parts.append(rel.encode("utf-8") + b"\0" + body + b"\0")
    return b"harden\0" + b"".join(h_parts)


def _norm_dir_text(path: Path) -> bytes:
    """All files under a directory, name-sorted, each EOL-normalized. Only
    defined for directories (rtl/, tb/, holdout/, ...); a file input raises
    so the caller's raw fallback takes over.

    `__pycache__` (and any stray `.pyc`/`.pyo`) is skipped: tb/ and holdout/
    are Python packages cocotb imports directly, and simply importing them
    once (no source edit at all) writes bytecode cache files under the very
    directory being hashed here - counted in, that would flip a gate's
    recorded input hash on its second run and mark a design gate stale for
    a reason that has nothing to do with the design."""
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    h_parts: list[bytes] = []
    for f in sorted(p for p in path.rglob("*")
                    if p.is_file() and "__pycache__" not in p.parts
                    and p.suffix not in (".pyc", ".pyo")):
        rel = f.relative_to(path).as_posix()
        h_parts.append(rel.encode("utf-8") + b"\0"
                       + _eol(_text(f.read_bytes())).encode("utf-8") + b"\0")
    return b"dir\0" + b"".join(h_parts)


def _norm_dir_bytes(path: Path) -> bytes:
    """dir_text without the EOL pass: name + raw bytes of every file, name-
    sorted. For big trees nobody hand-edits (the PDK's liberty), where
    decoding 330 MB of text buys nothing."""
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    h = hashlib.sha256()
    for f in sorted(p for p in path.rglob("*") if p.is_file()):
        h.update(f.relative_to(path).as_posix().encode("utf-8") + b"\0")
        with f.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        h.update(b"\0")
    return b"dirbytes\0" + h.digest()


def _norm_pex_netlists(path: Path) -> bytes:
    """The extracted netlists under a nested analog side's log/ the pex
    gates left, and nothing else log/ holds: a re-run of any other analog
    gate writes there too and changes no extraction."""
    if not path.is_dir():
        raise ValueError(f"{path} is not a directory")
    parts = [b"pex\0"]
    for sub in ("pex_sim", "pex_pvt"):
        for f in sorted((path / sub).glob("*.pex.ordered.spice")):
            parts.append(f"{sub}/{f.name}".encode() + b"\0"
                         + f.read_text(encoding="utf-8").replace(
                             "\r\n", "\n").encode("utf-8") + b"\0")
    return b"".join(parts)


NORMALIZERS = {
    "text_eol": _norm_text_eol,
    "dir_bytes": _norm_dir_bytes,
    "json_canonical": _norm_json_canonical,
    "json_canonical_sans_formal": _norm_json_canonical_sans_formal,
    "json_canonical_sans_roots": _norm_json_canonical_sans_roots,
    "harden_signoff": _norm_harden_signoff,
    "dir_text": _norm_dir_text,
    "pex_netlists": _norm_pex_netlists,
}

# Suffix -> normalizer for artifacts registered under non-standard names.
_SUFFIX_NORM = {
    ".json": "json_canonical",
    ".yaml": "json_canonical",
    ".yml": "json_canonical",
}


def norm_for_path(path: Path | str) -> str:
    """The norm a check stamps its input with (checklib.stamp): the norm of
    the artifact kind invalidation.yaml registers at that path (the longest
    registered path the input ends with), so the stamp is the hash
    record_gate compares it with; else by shape, a dir as dir_text."""
    p = Path(path)
    try:
        kinds = load_map()["artifact_kinds"].values()
    except (OSError, ValueError, KeyError, yaml.YAMLError):
        kinds = []
    tail = p.as_posix().rstrip("/")
    hits = [k for k in kinds if isinstance(k, dict) and k.get("path")
            and (tail == k["path"] or tail.endswith("/" + k["path"]))]
    if hits:
        return max(hits, key=lambda k: len(k["path"]))["norm"]
    if p.is_dir():
        return "dir_text"
    return _SUFFIX_NORM.get(p.suffix.lower(), "text_eol")


# ---------------------------------------------------------------------------
# the PDK (#138): kinds whose path starts ${PDK_ROOT}
# ---------------------------------------------------------------------------
PDK_PREFIX = "${PDK_ROOT}"
_PDK_ROOT_CACHE: list = []


def pdk_root() -> Path | None:
    """The toolchain's gf180mcuD tree, as layoutlib.pdk_root() resolves it
    (bin/eda --print-toolchain-root, so EDA_TOOLCHAIN moves it), or None
    when there is no toolchain - then every PDK kind hashes to None. Asked
    once per process."""
    if not _PDK_ROOT_CACHE:
        import subprocess
        eda = Path(__file__).resolve().parents[2] / "bin" / "eda"
        root = None
        try:
            proc = subprocess.run([str(eda), "--print-toolchain-root"],
                                  capture_output=True, text=True, timeout=30)
            if proc.returncode == 0 and proc.stdout.strip():
                root = Path(proc.stdout.strip()) / "foss" / "pdks" / "gf180mcuD"
        except (OSError, subprocess.SubprocessError):
            pass
        _PDK_ROOT_CACHE.append(root)
    return _PDK_ROOT_CACHE[0]


def is_pdk_path(rel: str) -> bool:
    return str(rel).startswith(PDK_PREFIX)


def resolve(ws: Path | str, rel: str) -> Path:
    """Where a kind's path is on disk: under the PDK for ${PDK_ROOT}/...,
    else under the workspace. With no toolchain a PDK path resolves to a
    path that does not exist (hash None)."""
    if is_pdk_path(rel):
        root = pdk_root()
        tail = str(rel)[len(PDK_PREFIX):].lstrip("/")
        return (root / tail) if root else Path("/nonexistent-pdk-root") / tail
    return Path(ws) / rel


def _stat_sig(p: Path) -> str:
    """Path, size and mtime of a file or of every file under a dir: what a
    PDK hash is cached on. The image's files are read-only; a new image, a
    ciel switch or an edited copy changes a path, size or mtime."""
    files = [p] if p.is_file() else sorted(f for f in p.rglob("*")
                                           if f.is_file())
    h = hashlib.sha256()
    for f in files:
        st = f.stat()
        h.update(f"{f}\0{st.st_size}\0{st.st_mtime_ns}\0".encode())
    return h.hexdigest()


def _pdk_cache_path() -> Path:
    import os
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "chip-flow" / "pdk_hashes.json"


def hash_pdk(path: Path, norm: str) -> str | None:
    """hash_artifact for a PDK path, cached on disk by (norm, path, stat
    signature): a freshness report must not re-read 330 MB of liberty."""
    if not path.exists():
        return None
    key = f"{norm}\0{path}\0{_stat_sig(path)}"
    cache_file = _pdk_cache_path()
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
        if not isinstance(cache, dict):
            cache = {}
    except (OSError, ValueError):
        cache = {}
    if key in cache:
        return cache[key]
    digest = hash_artifact(path, norm)
    cache = {k: v for k, v in cache.items()
             if not k.startswith(f"{norm}\0{path}\0")}
    cache[key] = digest
    try:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(f".{id(cache)}.tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(cache_file)
    except OSError:
        pass
    return digest


def pdk_version() -> dict:
    """{name, open_pdks, ciel, root} of the PDK this toolchain carries:
    open_pdks' commit from SOURCES, ciel's `current`. Values None where
    absent. What release writes into the attestation."""
    root = pdk_root()
    out = {"name": "gf180mcuD", "open_pdks": None, "ciel": None,
           "root": str(root) if root else None}
    if root is None:
        return out
    try:
        for line in (root / "SOURCES").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[0] == "open_pdks":
                out["open_pdks"] = parts[1]
    except OSError:
        pass
    try:
        out["ciel"] = (root.parent / "ciel" / "gf180mcu" / "current").read_text(
            encoding="utf-8").strip() or None
    except OSError:
        pass
    return out


def hash_artifact(path: Path | str, norm: str) -> str | None:
    """'<norm>:<sha256>' of the artifact's normalized content, None when the
    file/dir does not exist. Any normalizer failure (unparsable YAML/JSON,
    binary content) falls back to raw bytes under the 'raw' prefix - fail-safe:
    a raw hash only ever matches an identical file, so staleness can be
    over-reported but never missed. A harden output whose runs/ git doesn't
    carry answers from the harden's committed record (harden_record_hash)."""
    p = Path(path)
    if not p.exists():
        where = _in_harden_runs(p)
        return harden_record_hash(*where, norm) if where else None
    if norm == "harden_signoff" and p.is_dir():
        recorded = harden_record_hash(p, ".", norm)
        if recorded:
            return recorded
    fn = NORMALIZERS.get(norm)
    if fn is None:
        raise ValueError(f"unknown normalizer {norm!r}")
    try:
        out = fn(p)
    except Exception:
        if p.is_dir():  # unreadable dir member: hash the name list at least
            names = "\n".join(sorted(
                f.relative_to(p).as_posix() for f in p.rglob("*")))
            return "rawdir:" + hashlib.sha256(names.encode()).hexdigest()
        return "raw:" + hashlib.sha256(p.read_bytes()).hexdigest()
    return f"{norm}:{hashlib.sha256(out).hexdigest()}"


# ---------------------------------------------------------------------------
# workspace resolution + freshness
# ---------------------------------------------------------------------------
def find_workspace(input_file: Path | None,
                   explicit: str | None = None) -> Path | None:
    """The block workspace whose state.json a result/edit on `input_file`
    belongs in.

    An explicit --workspace wins and MUST hold a state.json (a typo that
    silently records nowhere is the bug this refusal exists to catch).
    Otherwise the input's own parents are walked - the pipeline always
    works on a file inside its workspace, so a caller that forgets the flag
    still records. A corpus input (evals/fixtures/..., a mutant, a scratch
    export) has no state.json above it and is left alone.
    """
    if explicit:
        ws = Path(explicit)
        if not (ws / "state.json").is_file():
            raise RuntimeError(f"--workspace {ws}: no state.json there")
        return ws
    if input_file is None:
        return None
    p = Path(input_file).resolve()
    for parent in list(p.parents)[:6]:
        if (parent / "state.json").is_file():
            return parent
    return None


# skills/msde/SKILL.md, "Run start": the nested workspaces take "block names
# `<name>` (digital - it is the tile) and `<name>_analog`". Never the
# directory's own name: layout_gen, analog LVS, pex_sim and top_harden name
# files after the block, so an `analog` block looks for netlist/analog.cir.
SPLIT_SUFFIX = {"digital": "", "analog": "_analog"}


def split_block_name(ws: Path | str) -> str | None:
    """The block name the msde split gives `ws`, or None when `ws` is not
    digital/ or analog/ directly under an msde workspace with a block."""
    ws = Path(ws)
    suffix = SPLIT_SUFFIX.get(ws.name)
    parent_state = ws.parent / "state.json"
    if suffix is None or not parent_state.is_file():
        return None
    try:
        parent = json.loads(parent_state.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if parent.get("skill") != "msde" or not parent.get("block"):
        return None
    return parent["block"] + suffix


def _in_harden_runs(path: Path) -> tuple[Path, str] | None:
    """(harden dir, path relative to it) for a path under some harden/runs/,
    None for any other path."""
    parts = path.parts
    for i in range(len(parts) - 1, 0, -1):
        if parts[i] == "runs" and parts[i - 1] == "harden":
            return Path(*parts[:i]), Path(*parts[i:]).as_posix()
    return None


def _harden_own_sha(harden_dir: Path) -> str:
    """Digest of a harden dir's own files, as harden_signoff folds them in."""
    return hashlib.sha256(
        _norm_harden_signoff(harden_dir, with_runs=False)).hexdigest()


def harden_record_hash(harden_dir: Path, rel: str, norm: str) -> str | None:
    """The hash a harden recorded for its output `rel` ("." for the harden
    dir as harden_signoff), so a clone, which has no runs/, reads the outputs
    the harden handed on (#239). Only with no runs/ at all, since real files
    are always what gets hashed, and only while the dir's own files are the
    ones that harden ran with. Any other case is None, and the caller hashes
    what is there: a hand-edited or missing record reads stale, never fresh."""
    if (harden_dir / "runs").exists():
        return None
    try:
        doc = json.loads((harden_dir / HARDEN_RECORD).read_text(encoding="utf-8"))
        if doc["own"] != _harden_own_sha(harden_dir):
            return None
        value = doc["outputs"][rel]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return value if (isinstance(value, str)
                     and value.startswith(norm + ":")) else None


def harden_record_targets(ws: Path, skill: str, gate: str, imap: dict,
                          registry: dict | None = None) -> dict[Path, dict]:
    """{harden dir: {rel: norm}} for the harden outputs `gate` writes, per
    gate_writes; {} for a gate that writes none."""
    out: dict[Path, dict] = {}
    for kind in (imap.get("gate_writes", {}).get(skill) or {}).get(gate) or []:
        norm = imap["artifact_kinds"][kind]["norm"]
        p = ws / kind_path(kind, imap, registry)
        where = (p, ".") if norm == "harden_signoff" else _in_harden_runs(p)
        if where:
            out.setdefault(where[0], {})[where[1]] = norm
    return out


def drop_harden_records(ws: Path, skill: str, gate: str, imap: dict,
                        registry: dict | None = None) -> None:
    """A harden is starting: the record of the last one vouches for nothing
    if this one doesn't pass."""
    for harden_dir in harden_record_targets(ws, skill, gate, imap, registry):
        (harden_dir / HARDEN_RECORD).unlink(missing_ok=True)


def write_harden_records(ws: Path, skill: str, gate: str, imap: dict,
                         registry: dict | None = None) -> list[str]:
    """The gate passed: record, next to its config, the hashes of the outputs
    it hands on and of the own files they came from. Deterministic and free
    of paths and times, so an unchanged re-harden leaves it as it was. An
    output the run didn't make gets no entry, and reads stale in a clone."""
    written = []
    targets = harden_record_targets(ws, skill, gate, imap, registry)
    for harden_dir, outputs in targets.items():
        if not (harden_dir / "runs").is_dir():
            continue
        hashes = {rel: hash_artifact(harden_dir / rel, norm)
                  for rel, norm in outputs.items()}
        record = harden_dir / HARDEN_RECORD
        record.write_text(json.dumps(
            {"own": _harden_own_sha(harden_dir),
             "outputs": {r: h for r, h in hashes.items() if h}},
            indent=2, sort_keys=True) + "\n", encoding="utf-8")
        written.append(record.relative_to(ws).as_posix())
    return written


def view_record_hash(ws: Path, kind: str, imap: dict) -> str | None:
    """The hash a passing gate recorded for a view git doesn't carry (a kind
    with `view_record` and `untracked`), so a clone reads the views that run
    used. Only with the whole untracked dir absent, since real files are
    always what gets hashed; a hand-edited or missing record reads stale,
    never fresh."""
    entry = imap["artifact_kinds"][kind]
    if "view_record" not in entry or (ws / entry["untracked"]).exists():
        return None
    try:
        value = json.loads(
            (ws / entry["view_record"]).read_text(encoding="utf-8"))[kind]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return value if (isinstance(value, str)
                     and value.startswith(entry["norm"] + ":")) else None


def drop_view_records(ws: Path, skill: str, gate: str, imap: dict) -> None:
    """The gate is starting: what its last pass recorded vouches for nothing
    if this run doesn't pass."""
    for kind in (imap["gate_inputs"].get(skill) or {}).get(gate) or []:
        record = imap["artifact_kinds"][kind].get("view_record")
        if record:
            (ws / record).unlink(missing_ok=True)


def write_view_records(ws: Path, skill: str, gate: str, imap: dict) -> list[str]:
    """The gate passed: record the views it read that git doesn't carry. Free
    of paths and times, so an unchanged re-run leaves it as it was. Hashes
    the files themselves, never an earlier record."""
    records: dict[str, dict] = {}
    for kind in gate_inputs_here(ws, skill, gate, imap):
        entry = imap["artifact_kinds"][kind]
        if "view_record" not in entry:
            continue
        hashed = hash_artifact(ws / entry["path"], entry["norm"])
        if hashed:
            records.setdefault(entry["view_record"], {})[kind] = hashed
    for rel, hashes in records.items():
        (ws / rel).parent.mkdir(parents=True, exist_ok=True)
        (ws / rel).write_text(json.dumps(hashes, indent=2, sort_keys=True)
                              + "\n", encoding="utf-8")
    return sorted(records)


def harden_outputs_changed(ws: Path, skill: str, gate: str, imap: dict,
                           registry: dict | None = None) -> list[dict]:
    """The harden outputs `gate` writes (gate_writes) whose files no longer
    hash to what its outputs.json recorded when it passed (#164): a LEF, SDF
    or SPEF swapped after the harden, which no gate's own inputs may hold.
    One {kind, path, record} per changed kind. With no runs/ the record is
    what every gate reads, so there is nothing to compare; with runs/ a
    missing or unreadable record vouches for nothing and every output there
    counts as changed."""
    changed = []
    for harden_dir, outputs in harden_record_targets(
            ws, skill, gate, imap, registry).items():
        if not (harden_dir / "runs").is_dir():
            continue
        record = harden_dir / HARDEN_RECORD
        try:
            recorded = json.loads(record.read_text(encoding="utf-8"))["outputs"]
            if not isinstance(recorded, dict):
                recorded = {}
        except (OSError, ValueError, KeyError, TypeError):
            recorded = {}
        for kind in (imap.get("gate_writes", {}).get(skill) or {}).get(gate) or []:
            norm = imap["artifact_kinds"][kind]["norm"]
            p = ws / kind_path(kind, imap, registry)
            where = (p, ".") if norm == "harden_signoff" else _in_harden_runs(p)
            if not where or where[0] != harden_dir:
                continue
            if hash_artifact(p, norm) != recorded.get(where[1]):
                changed.append({"kind": kind,
                                "path": p.relative_to(ws).as_posix(),
                                "record": record.relative_to(ws).as_posix()})
    return changed


def kind_path(kind: str, imap: dict, registry: dict | None = None) -> str:
    """Workspace-relative path for a standard artifact kind. A registry
    entry registered under the kind's name overrides the default template -
    but ONLY when the entry's own `kind` field matches (an unqualified
    name-match would silently hash the wrong file and MISS real staleness)."""
    if is_pdk_path(imap["artifact_kinds"][kind]["path"]):
        return imap["artifact_kinds"][kind]["path"]  # the PDK's, not the block's
    reg = (registry or {}).get(kind)
    if isinstance(reg, dict) and reg.get("path") and reg.get("kind") == kind:
        return reg["path"]
    return imap["artifact_kinds"][kind]["path"]


def hash_kind(ws: Path, kind: str, imap: dict,
              registry: dict | None = None) -> tuple[str, str | None]:
    """(workspace-relative path, hash-or-None) for a standard kind."""
    rel = kind_path(kind, imap, registry)
    norm = imap["artifact_kinds"][kind]["norm"]
    if is_pdk_path(rel):
        return rel, hash_pdk(resolve(ws, rel), norm)
    hashed = hash_artifact(ws / rel, norm)
    return rel, hashed if hashed is not None \
        else view_record_hash(ws, kind, imap)


def _cosim_corners_owed(ws: Path) -> bool:
    lib = str(Path(__file__).resolve().parent)
    if lib not in sys.path:
        sys.path.insert(0, lib)
    import cosim_corners  # noqa: PLC0415 - only asked for an msde cosim
    return cosim_corners.owed(ws)


# What a gate_inputs_when row may name: a workspace's answer, from its files.
INPUT_PREDICATES = {"cosim_corners_owed": _cosim_corners_owed}


def gate_inputs_here(ws: Path, skill: str, gate: str, imap: dict) -> list[str]:
    """The gate's row, without the kinds gate_inputs_when says this workspace
    does not read."""
    kinds = (imap["gate_inputs"].get(skill) or {}).get(gate) or []
    preds = ((imap.get("gate_inputs_when") or {}).get(skill) or {}).get(gate)
    skip = {k for pred, ks in (preds or {}).items()
            if not INPUT_PREDICATES[pred](ws) for k in ks}
    return [k for k in kinds if k not in skip]


def gate_input_hashes(ws: Path, skill: str, gate: str, imap: dict,
                      registry: dict | None = None) -> dict[str, str | None]:
    """{kind: hash|None} for every input the gate reads here; {} for a gate
    the map does not know (freshness then reports it 'unknown', never
    'fresh')."""
    return {k: hash_kind(ws, k, imap, registry)[1]
            for k in gate_inputs_here(ws, skill, gate, imap)}


def load_strengths(path: Path | str | None = None) -> dict:
    """{skill: {gate: strength}} from gates.yaml. Cached on (path, mtime). A
    row with no `strength` is 0; one that isn't a non-negative integer raises,
    since a typo there would silently keep weaker passes fresh."""
    p = Path(path) if path else DEFAULT_GATES
    key = (str(p), p.stat().st_mtime)
    if key in _STRENGTH_CACHE:
        return _STRENGTH_CACHE[key]
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    out: dict[str, dict[str, int]] = {}
    for skill, rows in (data.get("gates") or {}).items():
        out[skill] = {}
        for gate, row in (rows or {}).items():
            value = (row or {}).get("strength", 0)
            if not _is_strength(value):
                raise ValueError(f"{p}: gates.{skill}.{gate}.strength is "
                                 f"{value!r}, not a non-negative integer")
            out[skill][gate] = value
    _STRENGTH_CACHE[key] = out
    return out


def _is_strength(value) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)
            and value >= 0)


def gate_strength(skill: str, gate: str,
                  path: Path | str | None = None) -> int:
    """This gate's current strength; 0 for a gate gates.yaml doesn't list."""
    return (load_strengths(path).get(skill) or {}).get(gate, 0)


def views_of(facts: dict | None) -> dict[str, dict]:
    """The extracted views a gate result carries beyond its one nominal
    netlist (pex_sim's `rc_variants`, and its `fill` views, named
    fill_<mode>), each kept in the gate's record: name -> {path, sha256,
    pair} for an RC variant, {path, sha256, kind: fill} for a fill view."""
    views = {r["variant"]: {"path": r["netlist"], "sha256": r["sha256"],
                            "pair": r["pair"]}
             for r in (facts or {}).get("rc_variants") or []}
    for r in ((facts or {}).get("fill") or {}).get("views") or []:
        views[f"fill_{r['mode']}"] = {"path": r["netlist"],
                                      "sha256": r["sha256"], "kind": "fill"}
    return views


def view_changes(gate_entry: dict, ws: Path) -> list[str]:
    """`view:<name>` for each recorded view that no longer stands: its file on
    disk differs from the hash recorded, or the table now pairs it with another
    process (an RC variant; a fill view has no pair). A file that is not there
    (a clean checkout: log/ is not tracked) is not judged; the gate's inputs, which include the tech and the spec, vouch
    for what a re-extraction would give."""
    views = (gate_entry.get("last") or {}).get("views") or {}
    if not views:
        return []
    table = yaml.safe_load(RC_VARIANTS_PATH.read_text(encoding="utf-8"))
    pairs = (table or {}).get("variants") or {}
    return sorted(
        f"view:{name}" for name, v in views.items()
        if (v.get("kind") != "fill"
            and (pairs.get(name) or {}).get("pair") != v.get("pair"))
        or ((ws / v["path"]).exists()
            and hash_artifact(ws / v["path"], "text_eol") != v.get("sha256")))


def gate_freshness(gate_entry: dict, current: dict[str, str | None],
                   gate: str = "", strength: int = 0,
                   views_changed: list[str] | None = None) -> dict:
    """Verdict for one recorded gate entry against current hashes and the
    gate's current `strength` (gates.yaml).

    hash_valid: True (all recorded inputs match), False (>=1 differs),
                None (no recorded inputs - unknown gate or never recorded).
    fresh:      hash_valid is True AND no stale marks AND the record's
                strength is not below the gate's. A record with no strength
                (or an unreadable one) reads as 0.
    strength_stale: present only when the record's strength is below the
                gate's: the reason, naming the gate and both values.
    views_changed: view_changes' answer, which counts as changed inputs.
    """
    recorded = (gate_entry.get("last") or {}).get("inputs")
    marks = gate_entry.get("stale") or []
    if not isinstance(recorded, dict) or not recorded:
        return {"hash_valid": None, "changed_inputs": [],
                "stale_marks": marks, "fresh": False}
    # an input the map names now but the record never hashed (a kind added
    # to the gate's row after it ran - the PDK, a side spec) is unvouched
    # for: stale, not fresh (#113/#138)
    changed = sorted({k for k, v in recorded.items() if current.get(k) != v}
                     | (set(current) - set(recorded))
                     | set(views_changed or ()))
    hash_valid = not changed
    verdict = {"hash_valid": hash_valid, "changed_inputs": changed,
               "stale_marks": marks, "fresh": hash_valid and not marks}
    ran_at = (gate_entry.get("last") or {}).get("strength")
    ran_at = ran_at if _is_strength(ran_at) else 0
    if ran_at < strength:
        verdict["fresh"] = False
        verdict["strength_stale"] = (
            f"gate {gate} is now strength {strength}, but this result was "
            f"recorded at strength {ran_at}; re-run it")
    return verdict


def upstream_gates(skill: str, gate: str, imap: dict) -> list[str]:
    """The gates that write a kind this gate reads (gate_writes, #147),
    itself left out: the gates it comes after."""
    reads = set((imap["gate_inputs"].get(skill) or {}).get(gate) or [])
    writes = (imap.get("gate_writes") or {}).get(skill) or {}
    return sorted(u for u, kinds in writes.items()
                  if u != gate and reads & set(kinds or []))


def _propagate_upstream(gates_out: dict, data: dict, skill: str,
                        imap: dict) -> None:
    """Stale every recorded gate that comes after a recorded gate that is
    not fresh, or after one whose last run is later than its own (#147).
    A gate never recorded upstream has not re-run since, so it stales
    nothing; a gate with no recorded inputs stays `unknown`. Each stale
    gate gets `upstream_stale`: one reason per upstream gate."""
    recorded = data.get("gates") or {}
    done: dict[str, bool] = {}

    def ts(g: str) -> str:
        return str(((recorded.get(g) or {}).get("last") or {}).get("ts") or "")

    def fresh(g: str, seen: frozenset) -> bool:
        if g in done:
            return done[g]
        v = gates_out[g]
        reasons = []
        for u in upstream_gates(skill, g, imap):
            if u not in gates_out or u in seen:
                continue
            if not fresh(u, seen | {g}):
                reasons.append(f"upstream gate {u} is not fresh")
            elif ts(u) > ts(g):
                reasons.append(f"upstream gate {u} re-ran at {ts(u)}, after "
                               f"this result was recorded at {ts(g)}")
        if reasons and v["hash_valid"] is not None:
            v["upstream_stale"] = reasons
            v["fresh"] = False
        done[g] = bool(v["fresh"])
        return done[g]

    for g in gates_out:
        fresh(g, frozenset())


def freshness_report(data: dict, ws: Path,
                     imap: dict | None = None) -> dict:
    """Full freshness view of a state.json v3 payload.

    artifacts: every registered entry + every standard kind mapped for a
    recorded gate - {path, kind, exists, registered, current, changed,
    marks}. gates: per recorded gate the gate_freshness verdict + recorded/
    current hashes, and `upstream_stale` on a gate that is stale because a
    gate before it is (#147). summary.human_hold_pending = max human_hold over all
    outstanding marks (0 when everything is clean).
    """
    imap = imap or load_map()
    skill = data.get("skill") or ""
    registry = data.get("artifacts") or {}

    gates_out: dict[str, dict] = {}
    for gname, gentry in (data.get("gates") or {}).items():
        current = gate_input_hashes(ws, skill, gname, imap, registry)
        verdict = gate_freshness(gentry, current, gname,
                                 gate_strength(skill, gname),
                                 view_changes(gentry, ws))
        gates_out[gname] = {
            "status": gentry.get("status"),
            "recorded_inputs": (gentry.get("last") or {}).get("inputs"),
            "current_inputs": current,
            **verdict,
        }

    _propagate_upstream(gates_out, data, skill, imap)

    # artifacts: registered entries first, then any standard kind a recorded
    # gate reads that is not registered (visibility without registration)
    art_out: dict[str, dict] = {}
    for name, entry in registry.items():
        if not isinstance(entry, dict):        # pre-migration payload
            entry = {"path": str(entry)}
        rel = entry.get("path")
        kind = entry.get("kind")
        norm = (imap["artifact_kinds"][kind]["norm"]
                if kind in imap["artifact_kinds"]
                else norm_for_path(ws / rel) if rel else "text_eol")
        cur = ((hash_pdk(resolve(ws, rel), norm) if is_pdk_path(rel)
                else hash_artifact(ws / rel, norm)) if rel else None)
        reg_sha = entry.get("sha256")
        art_out[name] = {
            "path": rel, "kind": kind,
            "exists": bool(rel) and resolve(ws, rel).exists(),
            "registered": reg_sha, "current": cur,
            "changed": (reg_sha is not None or cur is not None)
                       and reg_sha != cur,
            "stale_marks": entry.get("stale") or [],
        }
    seen_kinds = {e.get("kind") for e in art_out.values()}
    for gname in gates_out:
        for kind in (imap["gate_inputs"].get(skill) or {}).get(gname, []):
            if kind in seen_kinds or kind in art_out:
                continue
            rel, cur = hash_kind(ws, kind, imap, registry)
            art_out[kind] = {
                "path": rel, "kind": kind,
                "exists": resolve(ws, rel).exists(),
                "registered": None, "current": cur, "changed": False,
                "stale_marks": [],
            }
            seen_kinds.add(kind)

    holds = [m.get("human_hold", 0)
             for g in gates_out.values() for m in g["stale_marks"]]
    holds += [m.get("human_hold", 0)
              for a in art_out.values() for m in a["stale_marks"]]
    fresh = sorted(g for g, v in gates_out.items() if v["fresh"])
    stale = sorted(g for g, v in gates_out.items()
                   if not v["fresh"] and v["hash_valid"] is not None)
    unknown = sorted(g for g, v in gates_out.items()
                     if v["hash_valid"] is None)
    return {
        "skill": skill,
        "gates": gates_out,
        "artifacts": art_out,
        "summary": {"fresh": fresh, "stale": stale, "unknown": unknown,
                    "human_hold_pending": max(holds, default=0)},
    }


# ---------------------------------------------------------------------------
# issue statuses
# ---------------------------------------------------------------------------
ISSUE_STATUSES = ("open", "fixing", "fixed", "escalated", "waived",
                  "superseded")
ISSUE_UNRESOLVED = ("open", "fixing", "escalated")


def superseded_closed(rec: dict, issues: list[dict]) -> bool:
    """True when a `superseded` issue's replacement (its `superseded_by`) is
    another issue of the same gate that is itself `fixed` - checked on the
    record every time, so a replacement reopened later reopens this one."""
    by = rec.get("superseded_by")
    for other in issues:
        if other is not rec and other.get("id") == by and by != rec.get("id"):
            return (other.get("gate") == rec.get("gate")
                    and other.get("status") == "fixed")
    return False


def issue_unresolved(rec: dict, issues: list[dict]) -> bool:
    """Whether an issue still counts as open: open/fixing/escalated always
    do (escalated is a human decision pending), and so does a `superseded`
    one whose replacement is not a fixed issue of the same gate. The one
    rule state.py resume, attest.py and task_router.py all count by."""
    status = rec.get("status")
    if status in ISSUE_UNRESOLVED:
        return True
    if status == "superseded":
        return not superseded_closed(rec, issues)
    return False
