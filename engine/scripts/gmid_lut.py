#!/usr/bin/env python
"""gmid_lut.py - write the gf180 3.3 V gm/Id lookup tables (#102).

    gmid_lut.py [--device D ...] [--corner C ...] [--out DIR] [--check]

Sweeps nfet_03v3 and pfet_03v3 (engine/lib/gmidlib.py's DEVICES) in
ngspice with the PDK's own models (design.ngspice plus sm141064.ngspice's
section for the corner: tt = typical at 27 C, ss at 125 C, ff at -40 C)
over L, VGS, VDS and VSB at W = 10 um, one nested DC sweep (VGS inside
VDS) per L and VSB, and writes each as pygmid's .mat
(gmidlib's docstring has the fields) with engine/reference/gmid's
manifest.json pinning every file's sha256, device and corner (the
tables go to --out, gmidlib.cache_dir() by default: only the manifest is
in git, see gmidlib).

The .mat bytes are reproducible (zlib, and the header's creation date is
replaced by a fixed text), so --check regenerates every table into a
scratch directory and compares its hash with the pin: a table that does
not regenerate to the same bytes, or a pin that is missing, is a finding.

Exit 0: written (or, with --check, every table matches its pin).
1: with --check, a table differs. 2: refused (no PDK, ngspice failed,
an unknown device or corner), with a remediation.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))

import numpy as np  # noqa: E402

import gmidlib  # noqa: E402
import layoutlib  # noqa: E402

W_UM = 10.0
L_UM = (0.28, 0.35, 0.5, 0.7, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0)
VSTEP_GS = 0.05
VSTEP_DS = 0.05
VMAX = 3.3
VSB = (0.0, 0.4, 0.8, 1.2)
INST = "m.xm1.m0"
# ngspice instance quantity -> what it is read for
SAVED = ("id", "vth", "gm", "gmbs", "gds", "cgg", "cgs", "cgd", "cgb",
         "cdd", "css", "cgso", "cgdo", "cgbo")
HEADER = b"MATLAB 5.0 MAT-file, chip-flow gmid_lut.py (gf180mcuD), reproducible"


def _grid(step: float) -> np.ndarray:
    n = int(round(VMAX / step))
    return np.round(np.arange(n + 1) * step, 6)


def deck(device: str, corner: str, pdk: Path, outdir: Path) -> str:
    section, temp_c = gmidlib.CORNERS[corner]
    p = device.startswith("p")
    s = -1 if p else 1
    lines = [f"* gmid_lut {device} {corner}",
             f".include '{pdk}/design.ngspice'",
             f".lib '{pdk}/sm141064.ngspice' {section}",
             f".temp {temp_c:g}",
             ".param lx=1u",
             "vd d 0 0", "vg g 0 0", "vb b 0 0",
             f"xm1 d g 0 b {device} w={W_UM:g}u l={{lx}}",
             ".option numdgt=8",
             ".save " + " ".join(f"@{INST}[{q}]" for q in SAVED),
             ".control"]
    vec = " ".join(f"@{INST}[{q}]" for q in SAVED)
    for il, l_um in enumerate(L_UM):
        lines += [f"alterparam lx={l_um:g}u", "reset"]
        for isb, vsb in enumerate(VSB):
            lines += [f"alter vb {-s * vsb:g}",
                      f"dc vg 0 {s * VMAX:g} {s * VSTEP_GS:g} "
                      f"vd 0 {s * VMAX:g} {s * VSTEP_DS:g}",
                      f"wrdata {outdir}/s_{il}_{isb}.txt {vec}",
                      "destroy all"]
    lines += [".endc", ".end"]
    return "\n".join(lines) + "\n"


def _read(path: Path, nvg: int, nvd: int) -> dict[str, np.ndarray]:
    rows = [r.split() for r in path.read_text().splitlines() if r.strip()]
    a = np.array(rows, dtype=float)
    if a.shape != (nvg * nvd, 2 * len(SAVED)):
        raise layoutlib.LayoutError(
            f"{path.name}: {a.shape} values, want {(nvg * nvd, 2 * len(SAVED))}")
    # wrdata writes (sweep value, vector) pairs; VGS is the inner sweep
    return {q: a[:, 2 * i + 1].reshape(nvd, nvg).T for i, q in enumerate(SAVED)}


def sweep(device: str, corner: str, work: Path) -> dict:
    pdk = layoutlib.pdk_root() / "libs.tech" / "ngspice"
    if not (pdk / "sm141064.ngspice").is_file():
        raise layoutlib.LayoutError(f"no gf180mcuD ngspice models at {pdk}")
    vgs, vds = _grid(VSTEP_GS), _grid(VSTEP_DS)
    cir = work / f"{device}_{corner}.cir"
    cir.write_text(deck(device, corner, pdk, work), encoding="utf-8")
    layoutlib.run_ngspice(cir, cwd=work, timeout=900)
    shape = (len(L_UM), len(vgs), len(vds), len(VSB))
    t = {f: np.zeros(shape) for f in gmidlib.FIELDS}
    for il in range(len(L_UM)):
        for isb in range(len(VSB)):
            r = _read(work / f"s_{il}_{isb}.txt", len(vgs), len(vds))
            put = {
                "ID": np.abs(r["id"]), "VT": np.abs(r["vth"]),
                "GM": r["gm"], "GMB": r["gmbs"], "GDS": r["gds"],
                "CGG": r["cgg"] + r["cgso"] + r["cgdo"] + r["cgbo"],
                "CGS": -r["cgs"] + r["cgso"], "CGD": -r["cgd"] + r["cgdo"],
                "CGB": -r["cgb"] + r["cgbo"], "CDD": r["cdd"] + r["cgdo"],
                "CSS": r["css"] + r["cgso"]}
            for f, v in put.items():
                t[f][il, :, :, isb] = v
    section, temp_c = gmidlib.CORNERS[corner]
    info = (f"gf180mcuD {device} W={W_UM:g}um, sm141064.ngspice {section}, "
            f"{temp_c:g} C, ngspice DC sweep, engine/scripts/gmid_lut.py")
    return {"INFO": info, "CORNER": corner.upper(),
            "TEMP": temp_c + 273.15, "NFING": 1.0, "W": W_UM,
            "L": np.array(L_UM), "VGS": vgs, "VDS": vds,
            "VSB": np.array(VSB), **t}


SIG = 6     # significant digits kept: the last bits of a solve never
            # reach the bytes, so the pin holds on another CPU


def _round(a: np.ndarray) -> np.ndarray:
    a = np.asarray(a, float)
    out = a.copy()
    nz = a != 0
    e = np.floor(np.log10(np.abs(a[nz])))
    q = 10.0 ** (SIG - 1 - e)
    out[nz] = np.round(a[nz] * q) / q
    return out


def write_mat(struct: dict, varname: str, path: Path) -> None:
    import scipy.io
    struct = {k: (_round(v) if isinstance(v, np.ndarray) else v)
              for k, v in struct.items()}
    scipy.io.savemat(str(path), {varname: struct}, do_compression=True,
                     oned_as="row")
    b = bytearray(path.read_bytes())
    b[:116] = HEADER.ljust(116)          # the header's date, made fixed
    path.write_bytes(bytes(b))


def build(devices, corners, out: Path, work: Path) -> dict:
    tables = {}
    for d in devices:
        for c in corners:
            sub = work / f"{d}_{c}"
            sub.mkdir(parents=True, exist_ok=True)
            name = gmidlib.table_name(d, c)
            write_mat(sweep(d, c, sub), name[:-4], out / name)
            section, temp_c = gmidlib.CORNERS[c]
            tables[name] = {"device": d, "corner": c, "section": section,
                            "temp_c": temp_c,
                            "sha256": gmidlib.sha256(out / name)}
    return tables


def grid_doc() -> dict:
    return {"W_um": W_UM, "L_um": list(L_UM),
            "VGS": [0.0, VMAX, VSTEP_GS], "VDS": [0.0, VMAX, VSTEP_DS],
            "VSB": list(VSB)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--device", action="append", choices=gmidlib.DEVICES)
    ap.add_argument("--corner", action="append", choices=tuple(gmidlib.CORNERS))
    ap.add_argument("--out", type=Path, default=None,
                    help="where the tables go (gmidlib.cache_dir())")
    ap.add_argument("--manifest", type=Path, default=gmidlib.MANIFEST)
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args(argv)
    devices = a.device or list(gmidlib.DEVICES)
    corners = a.corner or list(gmidlib.CORNERS)
    out = a.out or gmidlib.cache_dir()
    try:
        with tempfile.TemporaryDirectory(prefix="gmid_lut_") as tmp:
            tmp = Path(tmp)
            if a.check:
                doc = gmidlib.read_manifest(a.manifest)
                viol = []
                if doc.get("grid") != grid_doc():
                    viol.append({"grid": doc.get("grid"),
                                 "script": grid_doc()})
                made = build(devices, corners, tmp, tmp / "work")
                for name, row in made.items():
                    pin = doc.get("tables", {}).get(name, {}).get("sha256")
                    if pin != row["sha256"]:
                        viol.append({"table": name, "pinned": pin,
                                     "regenerated": row["sha256"]})
                print(json.dumps({"ok": not viol, "checked": sorted(made),
                                  "violations": viol}, indent=2))
                return 1 if viol else 0
            out.mkdir(parents=True, exist_ok=True)
            made = build(devices, corners, out, tmp / "work")
    except (layoutlib.LayoutError, gmidlib.GmidError, OSError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)[:600],
                          "remediation": "run through bin/chip-flow (the "
                          "toolchain's ngspice and gf180mcuD); see the "
                          "error for the deck that failed"}, indent=2))
        return 2
    mpath = a.manifest
    doc = json.loads(mpath.read_text()) if mpath.is_file() else {}
    doc.setdefault("tables", {}).update(made)
    doc["grid"] = grid_doc()
    doc["generator"] = "engine/scripts/gmid_lut.py"
    doc["note"] = ("pins only: the tables are made into gmidlib.cache_dir() "
                   "on first use and refused unless they hash to these")
    mpath.parent.mkdir(parents=True, exist_ok=True)
    mpath.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"ok": True, "written": sorted(made), "out": str(out),
                      "manifest": str(mpath)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
