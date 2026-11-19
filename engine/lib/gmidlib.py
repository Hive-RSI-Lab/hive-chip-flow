"""gmidlib.py - gm/Id lookup tables for the gf180mcuD 3.3 V MOSFETs (#102).

The tables are pygmid's .mat format (pygmid.Lookup reads them as they are):
one MATLAB struct per file whose fields are

    INFO, CORNER, TEMP (kelvin), NFING, W (um, the swept width),
    L (um), VGS, VDS, VSB (V, magnitudes: a pfet's are -VG, -VD, +VB)
    ID, VT, GM, GMB, GDS, CGG, CGS, CGD, CGB, CDD, CSS
        each shaped (len L, len VGS, len VDS, len VSB)

ID and VT are magnitudes for the pfet too (pygmid's own sign convention).
The capacitances are BSIM4's intrinsic ones with the overlap added (the
ngspice instance's cgso/cgdo/cgbo), each as a positive capacitance:
CGG = cgg + cgso + cgdo + cgbo, CGS = -cgs + cgso, CGD = -cgd + cgdo,
CGB = -cgb + cgbo, CDD = cdd + cgdo, CSS = css + cgso. Junction caps are
not in them (the sweep's instance has no ad/as): add the layout's.
CGS and CGB can dip slightly below zero at a few biases (near VDS = 0,
and in accumulation): BSIM4's terms are transcapacitances, not
branch capacitors, so read CGG for the gate's load.

engine/scripts/gmid_lut.py writes the tables and their manifest
(engine/reference/gmid/manifest.json: each table's fingerprint, device,
corner, temperature and the sweep's grid). Only the manifest is in git: a
table is 13 MB and takes ngspice about 4 s, so `load` makes a missing one
into the cache (cache_dir(): $CHIP_FLOW_CACHE/gmid, else chip-flow/gmid
in the user's cache) and then checks it like any other. It refuses a
table whose content or labels differ from the manifest's pin, so a table
made at another corner, or a re-labelled one, or one the toolchain no
longer reproduces, is never read as the one asked for.

The pin is the content, not the file's bytes: the same sweep compresses
to other bytes under another zlib build, and the last digit of a solve
can differ on another CPU. A fingerprint is each axis's first, last and
length, each field's shape, and every field's value at FP_N fixed
points; a table matches when every value is within FP_REL of the pin's
(a corner apart is tens of percent).
"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path

import numpy as np

REF = Path(__file__).resolve().parents[1] / "reference" / "gmid"
MANIFEST = REF / "manifest.json"
DEVICES = ("nfet_03v3", "pfet_03v3")
# corner -> (the sm141064.ngspice section, temperature in C)
CORNERS = {"tt": ("typical", 27.0), "ss": ("ss", 125.0), "ff": ("ff", -40.0)}
FIELDS = ("ID", "VT", "GM", "GMB", "GDS", "CGG", "CGS", "CGD", "CGB",
          "CDD", "CSS")


def cache_dir() -> Path:
    if os.environ.get("CHIP_FLOW_CACHE"):
        return Path(os.environ["CHIP_FLOW_CACHE"]) / "gmid"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "chip-flow" / "gmid"


class GmidError(Exception):
    """A table that is missing, unpinned, or not the one asked for."""


def table_name(device: str, corner: str) -> str:
    return f"gf180_{device}_{corner}.mat"


FP_N = 64
FP_REL = 1e-3
AXES = ("L", "VGS", "VDS", "VSB")


def fingerprint(t: dict) -> dict:
    """The content pin of table struct `t` (the module docstring)."""
    fp = {"axes": {k: [float(np.ravel(t[k])[0]), float(np.ravel(t[k])[-1]),
                       int(np.size(t[k]))] for k in AXES},
          "shape": list(np.shape(t["ID"])), "points": {}}
    for f in FIELDS:
        flat = np.ravel(np.asarray(t[f], float))
        idx = (np.arange(FP_N, dtype=np.int64) * 2654435761 + 12345) % flat.size
        fp["points"][f] = [float(f"{v:.6g}") for v in flat[idx]]
    return fp


def fingerprint_diff(pin: dict | None, got: dict) -> list[str]:
    """What differs between two fingerprints (empty: they match)."""
    if not isinstance(pin, dict) or "points" not in pin:
        return ["no fingerprint pinned"]
    out = []
    for k in AXES:
        a, b = pin.get("axes", {}).get(k), got["axes"][k]
        if a is None or a[2] != b[2] or not np.allclose(a[:2], b[:2],
                                                        atol=1e-9):
            out.append(f"axis {k}: pinned {a}, got {b}")
    if pin.get("shape") != got["shape"]:
        out.append(f"shape: pinned {pin.get('shape')}, got {got['shape']}")
    if out:
        return out
    for f in FIELDS:
        a = np.asarray(pin["points"].get(f, []), float)
        b = np.asarray(got["points"][f], float)
        if a.shape != b.shape:
            out.append(f"{f}: {a.size} pinned points, {b.size} here")
            continue
        tol = FP_REL * np.abs(a) + 1e-9 * max(float(np.abs(a).max()), 1e-30)
        bad = np.flatnonzero(np.abs(a - b) > tol)
        if bad.size:
            i = int(bad[np.argmax(np.abs(a - b)[bad] / (np.abs(a[bad]) + 1e-300))])
            out.append(f"{f}: {bad.size} of {a.size} points off, worst "
                       f"pinned {a[i]:.6g} got {b[i]:.6g}")
    return out


def read_manifest(manifest: Path = MANIFEST) -> dict:
    if not Path(manifest).is_file():
        raise GmidError(f"no manifest at {manifest}: run "
                        "engine/scripts/gmid_lut.py to write the tables")
    try:
        return json.loads(Path(manifest).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GmidError(f"{manifest}: {exc}") from exc


def _struct(path: Path) -> dict:
    import scipy.io
    mat = scipy.io.loadmat(str(path), matlab_compatible=True)
    keys = [k for k in mat if not k.startswith("__")]
    if len(keys) != 1:
        raise GmidError(f"{path}: {len(keys)} variables, want one struct")
    raw = mat[keys[0]]
    out = {}
    for name in raw.dtype.names:
        v = np.squeeze(raw[name][0][0])
        if v.dtype.kind in "US":     # a char array: one string
            out[name] = "".join(np.atleast_1d(v).ravel().tolist())
        else:
            out[name] = np.asarray(v, float)
    return out


def load(device: str, corner: str = "tt", ref: Path | None = None,
         manifest: Path = MANIFEST, make: bool = True) -> dict:
    """The table for `device` at `corner` from `ref` (cache_dir() by
    default), after its pin check: its fingerprint must match the
    manifest's, and its CORNER and TEMP be the corner's. A missing table is
    made first (gmid_lut.py's sweep) when `make`. Refuses (GmidError)
    otherwise."""
    if device not in DEVICES:
        raise GmidError(f"unknown device {device!r}: one of {DEVICES}")
    if corner not in CORNERS:
        raise GmidError(f"unknown corner {corner!r}: one of {tuple(CORNERS)}")
    ref = Path(ref) if ref is not None else cache_dir()
    name = table_name(device, corner)
    path = ref / name
    pins = read_manifest(manifest).get("tables", {})
    pin = pins.get(name)
    if pin is None:
        raise GmidError(f"{name} is not pinned in {manifest}")
    if not path.is_file():
        if not make:
            raise GmidError(f"{path} is missing: run "
                            "engine/scripts/gmid_lut.py --out " + str(ref))
        _make(device, corner, ref)
    try:
        t = _struct(path)
        diff = fingerprint_diff(pin.get("fingerprint"), fingerprint(t))
    except (KeyError, ValueError, TypeError, IndexError) as exc:
        diff = [f"not a gm/Id table: {exc}"]
    if diff:
        raise GmidError(f"{name} does not match its pin (a table made at "
                        "another corner, on another grid, or edited): "
                        + "; ".join(diff)[:400] + ". Delete it so load makes "
                        "it again, and if that still differs run "
                        "engine/scripts/gmid_lut.py --check (the toolchain "
                        "no longer reproduces the pin)")
    section, temp_c = CORNERS[corner]
    if (t.get("CORNER") != corner.upper()
            or not math.isclose(float(t["TEMP"]), temp_c + 273.15,
                                abs_tol=1e-6)
            or pin.get("corner") != corner or pin.get("device") != device):
        raise GmidError(f"{name}: labelled {t.get('CORNER')} at "
                        f"{float(t['TEMP']) - 273.15:g} C, the pin says "
                        f"{pin.get('device')} {pin.get('corner')}; asked "
                        f"for {device} {corner} ({temp_c:g} C)")
    t["path"] = str(path)
    return t


def _make(device: str, corner: str, ref: Path) -> None:
    import sys
    import tempfile
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import gmid_lut
    import layoutlib
    ref.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="gmid_", dir=ref) as tmp:
            gmid_lut.build([device], [corner], Path(tmp), Path(tmp) / "work")
            name = table_name(device, corner)
            os.replace(Path(tmp) / name, ref / name)
    except (layoutlib.LayoutError, OSError, ValueError) as exc:
        raise GmidError(f"making {device} {corner} into {ref}: {exc}") from exc


def _neighbours(values, x: float, what: str, log: bool = False):
    """The swept points either side of `x` with their weights (linear, or
    in log for L); one point when `x` is on the grid. Refuses an `x`
    outside the sweep."""
    v = np.asarray(values, float)
    if x < v[0] * (1 - 1e-9) - 1e-9 or x > v[-1] * (1 + 1e-9) + 1e-9:
        raise GmidError(f"{what}={x:g} is outside the table's "
                        f"{v[0]:g}..{v[-1]:g}")
    hi = int(np.clip(np.searchsorted(v, x - 1e-12), 0, len(v) - 1))
    if hi == 0 or math.isclose(v[hi], x, rel_tol=1e-6, abs_tol=1e-9):
        return [(hi, 1.0)]
    lo = hi - 1
    if log:
        w = (math.log(x) - math.log(v[lo])) / (math.log(v[hi])
                                                - math.log(v[lo]))
    else:
        w = (x - v[lo]) / (v[hi] - v[lo])
    return [(lo, 1 - w), (hi, w)]


def curve(t: dict, field: str, il: int, isb: int, vds: float) -> np.ndarray:
    """`field` against VGS at the swept L index `il` and VSB index `isb`,
    linear in VDS between its swept neighbours."""
    a = t[field]
    out = np.zeros(a.shape[1])
    for idd, wd in _neighbours(t["VDS"], vds, "VDS"):
        out += wd * a[il, :, idd, isb]
    return out


# the 3.3 V models' width bins in sm141064.ngspice (um, per device)
W_MIN_UM, W_MAX_UM = 0.22, 100.0
W_SPLIT_UM = 50.0       # a device wider than W_MAX is split into m of these
NARROW_UM = 1.0         # below this, narrow-width effects move the answer


def _size_at(t: dict, gmid: float, il: int, isb: int, vds: float,
             l_um: float) -> dict:
    """VGS and the per-width values at gm/Id = `gmid` on one swept L and
    VSB: where the gm/Id curve crosses `gmid` on its strong-inversion side
    (past its peak), found with a monotone cubic (pchip) in VGS."""
    from scipy.interpolate import PchipInterpolator

    vgs = t["VGS"]
    w_ref = float(t["W"])
    idc = curve(t, "ID", il, isb, vds)
    gm = curve(t, "GM", il, isb, vds)
    ok = idc > 0
    if ok.sum() < 4:
        raise GmidError("the table carries no current at that bias")
    g = np.where(ok, gm / np.where(ok, idc, 1), 0.0)
    peak = int(np.argmax(g))
    if gmid > g[peak]:
        raise GmidError(f"gm/Id={gmid:g} is above the device's peak "
                        f"{g[peak]:.3g} at L={l_um:g} um (weak inversion "
                        "limit)")
    xs, ys = vgs[peak:], g[peak:]
    if gmid < ys[-1]:
        raise GmidError(f"gm/Id={gmid:g} is below the table's lowest "
                        f"{ys[-1]:.3g} at VGS={xs[-1]:g} V")
    k = int(np.argmax(ys <= gmid))
    if ys[k] == gmid:
        v = float(xs[k])
    else:
        f = PchipInterpolator(xs, ys - gmid)
        lo, hi = float(xs[k - 1]), float(xs[k])
        for _ in range(60):
            mid = 0.5 * (lo + hi)
            if f(mid) > 0:
                lo = mid
            else:
                hi = mid
        v = 0.5 * (lo + hi)
    sel = slice(max(peak - 2, 0), None)

    def at(arr):
        return float(PchipInterpolator(vgs[sel], arr[sel])(v))

    cgg = curve(t, "CGG", il, isb, vds)
    idsafe = np.where(ok, idc, 1)
    return {"VGS": v, "log_ID_W": at(np.log(idsafe / w_ref)),
            "gds_id": at(curve(t, "GDS", il, isb, vds) / idsafe),
            "cgg_w": at(cgg / w_ref), "gm_cgg": at(gm / cgg)}


def size(t: dict, gmid: float, id_a: float, l_um: float, vds: float = 1.65,
         vsb: float = 0.0) -> dict:
    """W (um) and the small-signal values of one transistor at gm/Id =
    `gmid` (1/V), drain current `id_a` (A) and length `l_um`, at VDS and
    VSB. Each swept L and VSB either side of the asked one is solved on
    its own (_size_at), and their VGS, log Id/W, gds/Id, Cgg/W and gm/Cgg
    are then blended (L in log): neighbouring curves sit at different
    thresholds, so blending the curves first and solving after would
    size a device that is neither. The table's own W is the reference:
    a W far from it (narrow-width effects) is less exact, and a W past
    the models' widest bin comes back as m parallel devices of W_each_um.
    """
    rows = []
    for il, wl in _neighbours(t["L"], l_um, "L", log=True):
        for isb, ws in _neighbours(t["VSB"], vsb, "VSB"):
            rows.append((wl * ws, _size_at(t, gmid, il, isb, vds, l_um)))
    r = {k: sum(w * row[k] for w, row in rows) for k in rows[0][1]}
    id_w = math.exp(r["log_ID_W"])            # A/um
    w_um = id_a / id_w
    if w_um < W_MIN_UM:
        raise GmidError(f"W={w_um:.3g} um is below the models' narrowest "
                        f"{W_MIN_UM:g} um: raise Id, or lower gm/Id")
    m = 1 if w_um <= W_MAX_UM else math.ceil(w_um / W_SPLIT_UM)
    w_each = w_um / m
    warnings = []
    if w_each < NARROW_UM:
        warnings.append(f"W={w_each:.3g} um is narrow against the table's "
                        f"{float(t['W']):g} um: check it in ngspice")
    gm_s = gmid * id_a
    return {"W_um": w_um, "M": m, "W_each_um": w_each, "L_um": l_um,
            "VGS": r["VGS"], "VDS": vds, "VSB": vsb, "ID": id_a,
            "gm_id": gmid, "ID_W": id_w, "gm": gm_s,
            "gds": r["gds_id"] * id_a, "gm_gds": gmid / r["gds_id"],
            "Cgg": r["cgg_w"] * w_um, "fT": r["gm_cgg"] / (2 * math.pi),
            "W_ref_um": float(t["W"]), "warnings": warnings}
