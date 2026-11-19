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
(engine/reference/gmid/manifest.json: each file's sha256, device, corner,
temperature and the sweep's grid). Only the manifest is in git: a table
is 13 MB and takes ngspice about 4 s, so `load` makes a missing one into
the cache (cache_dir(): $CHIP_FLOW_CACHE/gmid, else chip-flow/gmid in the
user's cache) and then checks it like any other. It refuses a table
whose bytes or labels differ from the manifest's pin, so a table made at
another corner, or a re-labelled one, or one the toolchain no longer
reproduces, is never read as the one asked for.
"""
from __future__ import annotations

import hashlib
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


def sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def read_manifest(manifest: Path = MANIFEST) -> dict:
    if not Path(manifest).is_file():
        raise GmidError(f"no manifest at {manifest}: run "
                        "engine/scripts/gmid_lut.py to write the tables")
    return json.loads(Path(manifest).read_text(encoding="utf-8"))


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
    default), after its pin check: the file's sha256 must be the
    manifest's, and its CORNER and TEMP the corner's. A missing table is
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
    got = sha256(path)
    if got != pin["sha256"]:
        raise GmidError(f"{name}: sha256 {got[:12]} is not the pinned "
                        f"{pin['sha256'][:12]} (a table made elsewhere, at "
                        "another corner, or edited): regenerate it with "
                        "engine/scripts/gmid_lut.py, or restore the pinned one")
    t = _struct(path)
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
    except layoutlib.LayoutError as exc:
        raise GmidError(f"making {device} {corner}: {exc}") from exc


def _l_index(t: dict, l_um: float) -> tuple[int, int, float]:
    """The two swept L either side of `l_um` and the weight on the upper
    one (interpolated in log L); refuses an L outside the sweep."""
    ls = t["L"]
    if l_um < ls[0] * (1 - 1e-9) or l_um > ls[-1] * (1 + 1e-9):
        raise GmidError(f"L={l_um:g} um is outside the table's "
                        f"{ls[0]:g}..{ls[-1]:g} um")
    hi = int(np.searchsorted(ls, l_um * (1 - 1e-9)))
    hi = min(max(hi, 0), len(ls) - 1)
    if math.isclose(ls[hi], l_um, rel_tol=1e-6) or hi == 0:
        return hi, hi, 0.0
    lo = hi - 1
    w = (math.log(l_um) - math.log(ls[lo])) / (math.log(ls[hi])
                                                - math.log(ls[lo]))
    return lo, hi, w


def _axis(values, x: float, what: str) -> tuple[int, int, float]:
    v = np.asarray(values)
    if x < v[0] - 1e-9 or x > v[-1] + 1e-9:
        raise GmidError(f"{what}={x:g} is outside the table's "
                        f"{v[0]:g}..{v[-1]:g}")
    hi = int(np.clip(np.searchsorted(v, x - 1e-12), 0, len(v) - 1))
    if hi == 0 or math.isclose(v[hi], x, abs_tol=1e-9):
        return hi, hi, 0.0
    return hi - 1, hi, (x - v[hi - 1]) / (v[hi] - v[hi - 1])


def curve(t: dict, field: str, l_um: float, vds: float,
          vsb: float = 0.0) -> np.ndarray:
    """`field` against VGS at L, VDS and VSB: linear in VDS and VSB, log-L
    in L (each a weighted mean of the swept neighbours)."""
    a = t[field]
    li = _l_index(t, l_um)
    di = _axis(t["VDS"], vds, "VDS")
    si = _axis(t["VSB"], vsb, "VSB")
    out = np.zeros(a.shape[1])
    for il, wl in ((li[0], 1 - li[2]), (li[1], li[2])):
        for idd, wd in ((di[0], 1 - di[2]), (di[1], di[2])):
            for isb, ws in ((si[0], 1 - si[2]), (si[1], si[2])):
                w = wl * wd * ws
                if w:
                    out += w * a[il, :, idd, isb]
    return out


def size(t: dict, gmid: float, id_a: float, l_um: float, vds: float = 1.65,
         vsb: float = 0.0) -> dict:
    """W (um) and the small-signal values of one transistor at gm/Id =
    `gmid` (1/V), drain current `id_a` (A) and length `l_um`, at VDS and
    VSB. VGS is where the table's gm/Id curve crosses `gmid` on its
    strong-inversion side (past its peak), found with a monotone cubic
    (pchip) in VGS; Id/W, gds/Id and Cgg/W are read there the same way,
    Id/W in log. The table's own W is the reference: the values scale
    with W, so a W far from it (narrow-width effects) is less exact."""
    from scipy.interpolate import PchipInterpolator

    vgs = t["VGS"]
    w_ref = float(t["W"])
    idc = curve(t, "ID", l_um, vds, vsb)
    gm = curve(t, "GM", l_um, vds, vsb)
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
    # ys falls from the peak: the crossing as a root of the pchip
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

    def at(arr, log=False):
        y = np.log(arr[sel]) if log else arr[sel]
        r = float(PchipInterpolator(vgs[sel], y)(v))
        return math.exp(r) if log else r

    id_w = at(idc / w_ref, log=True)          # A/um
    gds_id = at(curve(t, "GDS", l_um, vds, vsb) / np.where(ok, idc, 1))
    cgg_w = at(curve(t, "CGG", l_um, vds, vsb) / w_ref)   # F/um
    gm_cgg = at(gm / curve(t, "CGG", l_um, vds, vsb))
    w_um = id_a / id_w
    gm_s = gmid * id_a
    cgg = cgg_w * w_um
    return {"W_um": w_um, "L_um": l_um, "VGS": v, "VDS": vds, "VSB": vsb,
            "ID": id_a, "gm_id": gmid, "ID_W": id_w, "gm": gm_s,
            "gds": gds_id * id_a, "gm_gds": gmid / gds_id, "Cgg": cgg,
            "fT": gm_cgg / (2 * math.pi), "W_ref_um": w_ref}
