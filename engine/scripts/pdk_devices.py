#!/usr/bin/env python
"""pdk_devices.py - round-trip every gf180mcuD primitive and write devices.yaml (#137).

    pdk_devices.py --out DIR [--models M ...] [--yaml F] [--check]
        [--netgen-setup F] [--jobs N] [--json F]

For each device in engine/lib/devicelib.py's CATALOG, at each catalogued
size: draw the PDK pcell (tapped and labelled, devicelib.build), run the TT
precheck's layer check on it, klayout DRC, magic extraction, and netgen LVS
against a one-device schematic; then run ngspice on the model at each of
its kind's model sections. One row per model goes to --yaml
(engine/reference/devices.yaml by default):

    model: {kind, pcell, sizes, drc_ok, extract_ok, lvs_ok, sim_ok,
            tt_legal, corner_kind, sections, reason, tt_reason}

A row is ok only when every size passed. `reason` names the first step that
failed and why; `tt_reason` the layers a TT tile may not have, and the
catalogue's `tt_note` when the failure is the TT rule itself, by design.

--check writes nothing: it reruns the round trip and compares each row's
flags with --yaml's, one violation per row that differs (the table is
reproducible, or it is wrong). --netgen-setup runs LVS under another netgen
setup file (the planted misspelt-class test).

Exit 0: the table was written (or, with --check, matches). 1: with --check,
a row differs. 2: refused (no PDK, an unknown model, a tool that did not
run), with a remediation.
"""
from __future__ import annotations

import argparse
import contextlib
import re
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "lib"))

import checklib  # noqa: E402
import devicelib  # noqa: E402
import layoutlib  # noqa: E402
import ttlib  # noqa: E402

FLAGS = devicelib.FLAGS

# ngspice probe per kind: the bias and the value printed. A model that
# loads and gives a finite non-zero value at every section is sim_ok.
_PROBE = {
    "mos_cap": ("v1 n1 0 dc 1.65 ac 1\n{inst}\n", "ac lin 1 1meg 1meg",
                "mag(i(v1))/(2*3.14159265*1e6)"),
    "mim": ("v1 n1 0 dc 0 ac 1\n{inst}\n", "ac lin 1 1meg 1meg",
            "mag(i(v1))/(2*3.14159265*1e6)"),
    "diode": ("v1 n1 0 dc 0.6\n{inst}\n", "op", "-i(v1)"),
    "bjt": ("v1 n1 0 dc 1\nv2 n2 0 dc 0.7\n{inst}\n", "op", "-i(v1)"),
}


def _sim_nodes(e: dict) -> list[str]:
    if e["kind"] != "bjt":
        return ["n1", "0"]
    if e["pins"] == ("C", "B", "E", "S"):
        return ["n1", "n2", "0", "0"]
    # pnp: C B E, the emitter at +0.7 against base and collector
    return ["0", "0", "n2"]


def _ngspice(model: str, size, section: str, work: Path) -> tuple[bool, str]:
    e = devicelib.CATALOG[model]
    pdk = layoutlib.pdk_root() / "libs.tech" / "ngspice"
    bias, analysis, expr = _PROBE[e["kind"]]
    if e["kind"] == "bjt" and len(e["pins"]) == 3:
        expr = "-i(v2)"
    inst = devicelib.instance_line(model, size, _sim_nodes(e))
    cir = work / f"sim_{section}.cir"
    cir.write_text(
        f"* {model} at {section}\n"
        f".include '{pdk / 'design.ngspice'}'\n"
        f".lib '{pdk / 'sm141064.spice'}' {section}\n"
        f"{bias.format(inst=inst)}"
        f".control\n{analysis}\nlet probe = {expr}\nprint probe\n.endc\n"
        f".end\n", encoding="utf-8")
    try:
        out = layoutlib.run_ngspice(cir, cwd=work, timeout=60)
    except layoutlib.LayoutError as exc:
        return False, f"ngspice at {section}: {str(exc)[:300]}"
    v = layoutlib.parse_ngspice_prints(out).get("probe")
    if v is None or v != v or v == 0 or abs(v) > 1:
        return False, f"ngspice at {section} gave probe={v}"
    return True, f"{v:.4g}"


_DEV = re.compile(r"^\s*([xdqcm]\S*)\s+(.*)$", re.I)


def _extracted_models(spice: str) -> list[str]:
    """The model (or subckt) name on every device line of the top cell."""
    names = []
    for line in spice.splitlines():
        m = _DEV.match(line)
        if not m:
            continue
        toks = [t for t in m.group(2).split() if "=" not in t]
        if toks:
            names.append(toks[-1].lower())
    return names


def round_trip(model: str, size, out: Path, setup: str | None) -> dict:
    """One device at one size: every step, each a flag and a reason."""
    e = devicelib.CATALOG[model]
    tag = devicelib.size_tag(model, size)
    work = out / tag
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    r = {"size": list(size), "reason": None, "tt_reason": None,
         **{f: False for f in FLAGS}}

    def fail(msg):
        r["reason"] = r["reason"] or msg

    # ngspice runs on the model alone, so it runs even if the drawing fails
    sim = []
    for section in devicelib.SECTIONS[e["kind"]]:
        ok, msg = _ngspice(model, size, section, work)
        sim.append(ok)
        if not ok:
            fail(msg)
    r["sim_ok"] = all(sim)

    try:
        # some pcells print layer tuples on stdout; keep the JSON clean
        with contextlib.redirect_stdout(sys.stderr):
            cell = devicelib.build(model, size, name=tag)
    except devicelib.DeviceError as exc:
        fail(f"build: {exc}")
        r["tt_reason"] = "not checked: nothing was drawn"
        return r
    gds = work / f"{tag}.gds"
    cell.write_gds(str(gds))

    probs = ttlib.gds_layer_problems(gds, layoutlib.pdk_root())
    r["tt_legal"] = not probs
    r["tt_reason"] = "; ".join(probs) or None

    layoutlib.run_klayout_drc(str(gds), tag, str(work / "drc.lyrdb"))
    cats = sorted({i["category"] for i in
                   layoutlib.parse_drc_rdb(work / "drc.lyrdb")})
    r["drc_ok"] = not cats
    if cats:
        fail(f"DRC: {', '.join(cats)}")

    spice, _ = layoutlib.run_magic_extract(str(work), str(gds), tag,
                                           parasitics=False)
    got = _extracted_models(Path(spice).read_text(encoding="utf-8"))
    r["extract_ok"] = got == [model.lower()]
    if not r["extract_ok"]:
        fail(f"magic extracts {got or 'no device'}, not one {model}")

    ref = work / "ref.spice"
    ref.write_text(devicelib.reference(model, size, tag), encoding="utf-8")
    try:
        ok, text = layoutlib.run_netgen_lvs(
            str(work), str(spice), tag, str(ref), tag, "lvs.log",
            setup=Path(setup) if setup else None)
    except layoutlib.LvsBlackBox as exc:
        # netgen ran: the setup has no class for the device (#137's
        # planted misspelt class), so LVS cannot see it - a failure
        fail(f"LVS: {str(exc)[:300]}")
        return r
    r["lvs_ok"] = bool(ok)
    if not ok:
        tail = [ln.strip() for ln in text.splitlines()
                if "mismatch" in ln.lower() or "do not match" in ln.lower()
                or "no such" in ln.lower()]
        fail("LVS: " + ("; ".join(tail[:3]) if tail else
                        "netgen reports the netlists do not match"))
    return r


def _one(job):
    model, size, out, setup = job
    try:
        return model, round_trip(model, size, Path(out), setup)
    except (layoutlib.LayoutError, OSError) as exc:
        return model, {"size": list(size), "error": str(exc)[:500]}


def table(models: list[str], out: Path, setup: str | None,
          jobs: int) -> tuple[dict, list[str]]:
    work = [(m, tuple(s), str(out), setup)
            for m in models for s in devicelib.CATALOG[m]["sizes"]]
    with ProcessPoolExecutor(max_workers=jobs) as pool:
        results = list(pool.map(_one, work))
    errors = [f"{m} {r['size']}: {r['error']}" for m, r in results
              if "error" in r]
    rows = {}
    for m in models:
        e = devicelib.CATALOG[m]
        runs = [r for mm, r in results if mm == m and "error" not in r]
        if len(runs) != len(e["sizes"]):
            continue
        row = {"kind": e["kind"], "pcell": e["pcell"],
               "sizes": [r["size"] for r in runs]}
        for f in FLAGS:
            row[f] = all(r[f] for r in runs)
        row["corner_kind"] = devicelib.SECTIONS[e["kind"]][0].split("_")[0]
        row["sections"] = list(devicelib.SECTIONS[e["kind"]])
        row["reason"] = next((f"{devicelib.size_tag(m, r['size'])}: "
                              f"{r['reason']}" for r in runs
                              if r["reason"]), None)
        row["tt_reason"] = next((r["tt_reason"] for r in runs
                                 if r["tt_reason"]), None)
        if row["tt_reason"] and e.get("tt_note"):
            row["tt_reason"] += f" - {e['tt_note']}"
        rows[m] = row
    return rows, errors


def run(args) -> dict:
    if not layoutlib.pdk_root().is_dir():
        raise checklib.CheckError(f"no gf180mcuD PDK at {layoutlib.pdk_root()}")
    models = args.models or list(devicelib.CATALOG)
    unknown = [m for m in models if m not in devicelib.CATALOG]
    if unknown:
        raise checklib.CheckError(
            f"not catalogued in devicelib.CATALOG: {', '.join(unknown)}")
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    rows, errors = table(models, out, args.netgen_setup, args.jobs)
    if errors:
        raise checklib.CheckError("a tool did not run: " + " | ".join(errors))
    yml = Path(args.yaml) if args.yaml else devicelib.DEVICES_YAML
    violations = []
    if args.check:
        have = devicelib.load(yml)
        for m, row in rows.items():
            old = have.get(m.lower())
            diff = ({f: (old.get(f), row[f]) for f in FLAGS
                     if old.get(f) != row[f]} if old else {"row": "missing"})
            if diff:
                violations.append(checklib.violation(
                    "pdk_devices", "error", str(yml), m, "row_differs", [m],
                    f"{m}: devices.yaml says {diff} (table, rerun)",
                    "pdk_devices", diff=diff))
    else:
        import yaml
        have = devicelib.load(yml) if args.models else {}
        have.update(rows)
        yml.parent.mkdir(parents=True, exist_ok=True)
        yml.write_text(
            "# gf180mcuD primitive devices: written by "
            "engine/scripts/pdk_devices.py (#137). Do not edit by hand.\n"
            + yaml.safe_dump({"devices": dict(sorted(have.items()))},
                             sort_keys=False, width=100),
            encoding="utf-8")
    passed = sorted(m for m, r in rows.items() if all(r[f] for f in FLAGS))
    return checklib.report(
        "pdk_devices", None, violations, yaml=str(yml), models=len(rows),
        passed=passed, failed={m: r["reason"] or r["tt_reason"]
                               for m, r in rows.items() if m not in passed},
        rows=rows)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True)
    ap.add_argument("--models", nargs="+")
    ap.add_argument("--yaml")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--netgen-setup")
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--json")
    args = ap.parse_args(argv)
    try:
        return checklib.emit(run(args), args.json)
    except (checklib.CheckError, layoutlib.LayoutError) as exc:
        return checklib.emit({"script": "pdk_devices", "status": "error",
                              "error": str(exc), "remediation": str(exc)},
                             args.json)


if __name__ == "__main__":
    sys.exit(main())
