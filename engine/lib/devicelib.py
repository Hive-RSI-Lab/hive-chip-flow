"""gf180mcuD primitive devices: what each one is, how to draw a labelled
cell of it, and what the device-coverage run found (#137).

`engine/scripts/pdk_devices.py` draws every device in CATALOG, runs it
through klayout DRC, magic extraction, netgen LVS against a one-device
schematic, an ngspice run at each of its kind's model sections and the TT
precheck's layer check, and writes the result to DEVICES_YAML. This module
holds the parts the script, layoutlib's wrappers and the lint gates share:

- CATALOG: model -> kind, pcell, pins, and how to read the pins off the
  pcell's own metal1 pads;
- build(): the drawn, tapped and labelled cell for one device and size;
- reference(): its one-device LVS schematic;
- load(), lvs_problem(), tt_problem() and not_ready(): a devices.yaml
  row, and why a model may not be used (not lvs_ok in a netlist, not
  tt_legal in a TT tile, or not passing everything in a layoutlib cell).

A pin is found on the pcell's geometry, never at a typed-in coordinate: a
pad is one merged metal1 shape, classed by what its contacts land on
(poly, N+ or P+ comp) and the well under them (nwell, LVPW, DNW). A pin
whose pad the pcell does not draw is a build failure with that reason, and
the row records it.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import yaml

import layoutlib

HERE = Path(__file__).resolve().parent
DEVICES_YAML = HERE.parent / "reference" / "devices.yaml"

KINDS = ("fet", "res", "mim", "mos_cap", "diode", "bjt")

# The model sections each kind loads from sm141064.spice (the .lib names).
SECTIONS = {
    "mos_cap": ("moscap_typical", "moscap_ss", "moscap_ff"),
    "diode": ("diode_typical", "diode_ss", "diode_ff"),
    "bjt": ("bjt_typical", "bjt_ss", "bjt_ff"),
    "mim": ("mimcap_typical", "mimcap_ss", "mimcap_ff"),
}

_L = {"metal1": (34, 0), "contact": (33, 0), "poly": (30, 0),
      "comp": (22, 0), "nplus": (32, 0), "pplus": (31, 0),
      "nwell": (21, 0), "dnwell": (12, 0), "lvpw": (204, 0)}


def _moscap(model, mos, volt, pads, tap):
    return {"kind": "mos_cap", "pcell": "draw_cap_mos.draw_cap_mos",
            "args": {"type": mos, "volt": volt},
            "size_keys": ("lc", "wc"), "sizes": ((2.0, 2.0), (4.0, 2.0)),
            "pins": ("G", "SD") + ((tap[1],) if tap else ()),
            "pads": pads, "tap": tap}


def _diode(fn, volt, pads, tap=None, extra=None):
    args = {"cw": 0.36}
    if volt:
        args["volt"] = volt
    args.update(extra or {})
    return {"kind": "diode", "pcell": f"draw_diode.{fn}", "args": args,
            "size_keys": ("la", "wa"), "sizes": ((2.0, 2.0), (4.0, 2.0)),
            "pins": ("A", "K"), "pads": pads, "tap": tap}


def _bjt(dev, pins):
    return {"kind": "bjt", "pcell": f"bjt/{dev}.gds", "args": {},
            "size_keys": (), "sizes": ((),), "pins": pins, "pads": {},
            "tap": None}


# model -> entry. `pads` maps a pad class to its pin; `tap` adds a psub
# ("p") or nwell ("n") tap carrying the named pin, for a terminal the
# pcell draws no pad for.
CATALOG: dict[str, dict] = {}
for _v, _s in (("3.3V", "03v3"), ("5/6V", "06v0")):
    CATALOG[f"cap_nmos_{_s}"] = _moscap(
        f"cap_nmos_{_s}", "cap_nmos", _v,
        {"poly": "G", "n@lvpw": "SD", "n": "SD"}, ("p", "B"))
    CATALOG[f"cap_pmos_{_s}"] = _moscap(
        f"cap_pmos_{_s}", "cap_pmos", _v, {"poly": "G", "p@nw": "SD"},
        ("n", "W"))
    CATALOG[f"cap_nmos_{_s}_b"] = _moscap(
        f"cap_nmos_{_s}_b", "cap_nmos_b", _v, {"poly": "G", "n@nw": "SD"},
        ("p", "B"))
    CATALOG[f"cap_pmos_{_s}_b"] = _moscap(
        f"cap_pmos_{_s}_b", "cap_pmos_b", _v, {"poly": "G", "p": "SD"},
        None)
for _v, _s in (("3.3V", "03v3"), ("5/6V", "06v0")):
    CATALOG[f"diode_nd2ps_{_s}"] = _diode(
        "draw_diode_nd2ps", _v, {"p@lvpw": "A", "p": "A", "n@lvpw": "K",
                                 "n": "K"})
    CATALOG[f"diode_pd2nw_{_s}"] = _diode(
        "draw_diode_pd2nw", _v, {"p@nw": "A", "n@nw": "K"})
    CATALOG[f"diode_nw2ps_{_s}"] = _diode(
        "draw_diode_nw2ps", _v, {"p": "A", "n@nw": "K"})
CATALOG["diode_pw2dw"] = _diode("draw_diode_pw2dw", "3.3V",
                                {"p@dnw": "A", "n@dnw": "K"})
CATALOG["diode_dw2ps"] = _diode("draw_diode_dw2ps", "3.3V", {"n@dnw": "K"},
                                tap=("p", "A"))
CATALOG["sc_diode"] = _diode("draw_sc_diode", None,
                             {"?@dnw": "A", "n@dnw": "K"}, extra={"m": 1})
for _d in ("npn_10p00x10p00", "npn_05p00x05p00", "npn_00p54x16p00",
           "npn_00p54x08p00", "npn_00p54x04p00", "npn_00p54x02p00"):
    CATALOG[_d] = _bjt(_d, ("C", "B", "E", "S"))
for _d in ("pnp_10p00x10p00", "pnp_05p00x05p00", "pnp_10p00x00p42",
           "pnp_05p00x00p42"):
    CATALOG[_d] = _bjt(_d, ("C", "B", "E"))

# MIM-B between metal4 and metal5: the MIM the PLL draws. Its plates are
# not metal1 pads, so _mim_cell labels them (T on metal5, B on metal4).
CATALOG["cap_mim_2f0_m4m5_noshield"] = {
    "kind": "mim", "pcell": "draw_cap_mim.draw_cap_mim",
    "args": {"mim_option": "MIM-B", "metal_level": "M5"},
    "size_keys": ("lc", "wc"), "sizes": ((10.0, 10.0), (20.0, 10.0)),
    "pins": ("T", "B"), "pads": {}, "tap": None,
    "plates": {"T": ((81, 0), (81, 10)), "B": ((46, 0), (46, 10))}}


class DeviceError(RuntimeError):
    """A device that cannot be drawn as asked (unknown model, a pin the
    pcell draws no pad for)."""


def size_tag(model: str, size) -> str:
    e = CATALOG[model]
    if not e["size_keys"]:
        # never the device's own name: the imported PDK cell has it, and
        # netgen crashes on a top cell named after its device
        return model + "_cell"
    return model + "_" + "x".join(f"{v:g}".replace(".", "p") for v in size)


def _pad_classes(comp) -> list[tuple[str, float, float]]:
    """(class, x, y) for every merged metal1 pad of comp: poly, n, p or ?
    (a pad with no contact on poly or on N+/P+ comp: the Schottky anode's
    metal), with @nw / @lvpw / @dnw for the well under its contacts."""
    import klayout.db as kdb

    def region(name):
        return kdb.Region(comp.kdb_cell.begin_shapes_rec(
            comp.kcl.layer(*_L[name])))

    r = {k: region(k) for k in _L}
    out = []
    for pad in r["metal1"].merged().each():
        pr = kdb.Region(pad)
        cs = r["contact"] & pr
        if not (cs & r["poly"]).is_empty():
            k = "poly"
        elif not (cs & r["comp"] & r["nplus"]).is_empty():
            k = "n"
        elif not (cs & r["comp"] & r["pplus"]).is_empty():
            k = "p"
        else:
            k = "?"
        probe = cs if not cs.is_empty() else pr
        if k != "poly":
            for well, tag in (("nwell", "@nw"), ("lvpw", "@lvpw"),
                              ("dnwell", "@dnw")):
                if not (probe & r[well]).is_empty():
                    k += tag
                    break
        c = (next(probe.each()).bbox().center()
             .to_dtype(comp.kcl.dbu))
        out.append((k, c.x, c.y))
    return out


def _bjt_cell(model: str, name: str):
    """The PDK's fixed BJT layout, relabelled: its own 34/10 pin texts
    (I1_default_C ...) become plain C, B, E, S at the same points."""
    import gdsfactory as gf
    import klayout.db as kdb

    gds = (layoutlib.pdk_root() / "libs.tech" / "klayout" / "tech" /
           "pymacros" / "cells" / "bjt" / f"{model}.gds")
    if not gds.is_file():
        raise DeviceError(f"no PDK layout for {model} at {gds}")
    ly = kdb.Layout()
    ly.read(str(gds))
    top_cell = ly.top_cell()
    seen: dict[str, tuple[float, float]] = {}
    for it in top_cell.begin_shapes_rec(ly.layer(34, 10)):
        sh = it.shape()
        if not sh.is_text():
            continue
        pin = sh.text.string.replace("I1_default_", "")
        if pin not in seen:
            p = it.trans() * sh.text.trans.disp
            seen[pin] = (p.x * ly.dbu, p.y * ly.dbu)
    missing = [p for p in CATALOG[model]["pins"] if p not in seen]
    if missing:
        raise DeviceError(f"{model}'s PDK layout labels no "
                          f"{', '.join(missing)} pin")
    top = gf.Component()
    top.add_ref(gf.import_gds(str(gds)))
    labels = [(p, *seen[p], layoutlib.GF180_LAYER["metal1_label"])
              for p in CATALOG[model]["pins"]]
    return layoutlib.finalize(top, name, labels)


def _mim_cell(model: str, size, name: str):
    """The PDK MIM pcell with a label on each plate: T at the top plate's
    centre, B on the bottom plate's margin outside the top plate."""
    import gdsfactory as gf

    e = CATALOG[model]
    layoutlib.gf180_cells()
    mod_name, fn = e["pcell"].split(".")
    draw = getattr(importlib.import_module(f"cells.{mod_name}"), fn)
    cell = draw(**e["args"], **dict(zip(e["size_keys"], size)))
    (tlay, tlab), (blay, blab) = e["plates"]["T"], e["plates"]["B"]
    tb = layoutlib.layer_boxes(cell, tlay)
    bb = layoutlib.layer_boxes(cell, blay)
    if not tb or not bb:
        raise DeviceError(f"{model}'s pcell draws no plate on "
                          f"{tlay if not tb else blay}")
    t, b = tb[0], max(bb, key=lambda x: (x[2] - x[0]) * (x[3] - x[1]))
    top = gf.Component()
    top.add_ref(cell)
    labels = [("T", (t[0] + t[2]) / 2, (t[1] + t[3]) / 2, tlab),
              ("B", (b[0] + t[0]) / 2, (b[1] + b[3]) / 2, blab)]
    return layoutlib.finalize(top, name, labels)


def build(model: str, size=None, name: str | None = None):
    """The finalized, labelled cell for one device: the PDK pcell at
    `size` (the entry's size_keys, e.g. (lc, wc) in um; the first catalog
    size when None), a tap for a terminal the pcell has no pad for, and a
    metal1 label on each pin's pad. Raises DeviceError when the pcell
    draws no pad for one of the pins."""
    import gdsfactory as gf

    if model not in CATALOG:
        raise DeviceError(f"{model} is not a catalogued gf180mcuD device")
    e = CATALOG[model]
    size = tuple(size) if size is not None else e["sizes"][0]
    name = name or size_tag(model, size)
    if e["kind"] == "bjt":
        return _bjt_cell(model, name)
    if e["kind"] == "mim":
        return _mim_cell(model, size, name)
    layoutlib.gf180_cells()
    mod_name, fn = e["pcell"].split(".")
    draw = getattr(importlib.import_module(f"cells.{mod_name}"), fn)
    cell = draw(**e["args"], **dict(zip(e["size_keys"], size)))
    top = gf.Component()
    top.add_ref(cell)
    labels = []
    lab = layoutlib.GF180_LAYER["metal1_label"]
    if e["tap"]:
        kind, pin = e["tap"]
        b = top.dbbox()
        tsz = 1.0
        x0, y0 = b.left - 1.5 - tsz, b.center().y - tsz / 2
        tap = top.add_ref(layoutlib.psub_tap(tsz) if kind == "p"
                          else layoutlib.nwell_tap(tsz))
        tap.dmove((x0, y0))
        if kind == "n":
            # extend the pcell's own nwell over the tap (DF.4d_LV: an
            # nwell tap needs its well's full enclosure)
            nb = layoutlib.layer_boxes(cell, _L["nwell"])[0]
            layoutlib.rect(top, x0 - 1.0, min(nb[1], y0 - 1.0), nb[2],
                           max(nb[3], y0 + tsz + 1.0), _L["nwell"])
        labels.append((pin, x0 + tsz / 2, y0 + tsz / 2, lab))
    placed = {l[0] for l in labels}
    for k, x, y in _pad_classes(top):
        pin = e["pads"].get(k)
        if pin and pin not in placed:
            labels.append((pin, x, y, lab))
            placed.add(pin)
    missing = [p for p in e["pins"] if p not in placed]
    if missing:
        raise DeviceError(
            f"{model}'s pcell ({e['pcell']}) draws no metal1 pad for pin "
            f"{', '.join(missing)}; found pads "
            f"{sorted({k for k, _, _ in _pad_classes(top)})}")
    return layoutlib.finalize(top, name, labels)


def instance_line(model: str, size, nodes) -> str:
    """The one SPICE line that calls `model` at `size` on `nodes` (the
    model's own terminal order: G SD for a MOS cap, A K for a diode,
    C B E [S] for a BJT)."""
    e = CATALOG[model]
    n = " ".join(nodes)
    if e["kind"] in ("mos_cap", "mim"):
        lc, wc = size
        return f"xc1 {n} {model} c_length={lc:g}u c_width={wc:g}u"
    if e["kind"] == "diode":
        la, wa = size
        return (f"d1 {n} {model} area={la * wa:g}p "
                f"pj={2 * (la + wa):g}u")
    return f"xq1 {n} {model}"


def reference(model: str, size, name: str) -> str:
    """The one-device LVS schematic: subckt `name` on the device's pins
    (a tap pin is a port of its own, wired to nothing in the device)."""
    e = CATALOG[model]
    dev = e["pins"][:2] if e["kind"] != "bjt" else e["pins"]
    return (f".subckt {name} {' '.join(e['pins'])}\n"
            f"{instance_line(model, size, dev)}\n.ends\n")


def load(path: Path | None = None) -> dict[str, dict]:
    """devices.yaml's rows, model (lower-case) -> row. Empty when the
    file does not exist."""
    p = Path(path) if path else DEVICES_YAML
    if not p.is_file():
        return {}
    data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {str(k).lower(): v for k, v in (data.get("devices") or {}).items()}


FLAGS = ("drc_ok", "extract_ok", "lvs_ok", "sim_ok", "tt_legal")


def _row(model: str, table: dict | None) -> dict | None:
    rows = load() if table is None else table
    return rows.get(model.lower())


def lvs_problem(model: str, table: dict | None = None) -> str | None:
    """Why `model` cannot go to layout, or None: its devices.yaml row is
    not lvs_ok. A model the table has no row for is not judged here (the
    FETs and resistors have their own gates)."""
    row = _row(model, table)
    if row is None or row.get("lvs_ok"):
        return None
    return (f"{model} is not lvs_ok in devices.yaml: "
            f"{row.get('reason') or 'its round trip failed'}")


def tt_problem(model: str, table: dict | None = None) -> str | None:
    """Why `model` cannot go in a TT tile, or None: its devices.yaml row
    is not tt_legal. No row: not judged here."""
    row = _row(model, table)
    if row is None or row.get("tt_legal"):
        return None
    return (f"{model} is not tt_legal in devices.yaml: "
            f"{row.get('tt_reason') or 'the TT layer check failed'}")


def not_ready(model: str, table: dict | None = None) -> str | None:
    """Why layoutlib will not draw `model`, or None: it must have a
    devices.yaml row with every flag true (DRC, extraction, LVS, ngspice
    and the TT layer check all passed at every catalogued size)."""
    row = _row(model, table)
    if row is None:
        return (f"{model} has no devices.yaml row: run "
                f"engine/scripts/pdk_devices.py --models {model}")
    bad = [f for f in FLAGS if not row.get(f)]
    if bad:
        return (f"{model} failed its round trip ({', '.join(bad)}): "
                f"{row.get('reason') or row.get('tt_reason')}")
    return None
