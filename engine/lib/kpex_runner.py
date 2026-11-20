#!/usr/bin/env python
"""kpex_runner.py - klayout_pex (kpex) 2.5D on GF180, with gate poly as a
conductor. Run by kpexlib.run_kpex inside the toolchain:

    eda python kpex_runner.py DECK_DIR <kpex arguments...>

kpex 0.4's GF180 deck and tech file call poly2 a conductor only off the
channel (`poly_con`), so the gate poly of every transistor is not part of
its net and every capacitance between a metal and the gate below it is lost:
on a current mirror that halves the coupling of its two nets. This copies
the deck into DECK_DIR and splits poly2 into `poly2_wire` (off the channel,
the old poly_con) and `poly2_gate` (over the channel), both joined to the
net. The gate poly gets the coverage magic's own tech gives it: a metal
above it couples to it (area and the metal edge's fringe, the same values as
over wire poly), and it has no C of its own to the substrate or from its
own edges (magic's `*poly` sidewall, perimeter and fringe exclude the fets;
the channel is the device's own model). kpex divides by a capacitance, so a
1e-6 aF/um^2 entry stands for "none". The gate sits just below Poly2 in the
process stack, so a wire poly edge's fringe to the substrate is shielded by
the gate it ends on rather than counted over the channel.

The tech file also files metal5 (81/0) under "MetalTop", which its
capacitance tables know only as a substrate plate, so any layout with metal5
beside another conductor crashed kpex on a KeyError; it is filed under
"Metal5", the name its process stack and tables use. Its cap layer is
metal5 off the MIM top plate (`metal5_ncap`, metal5 not over fusetop): over
the plate metal5 is the plate's strap, and counting its area over the metal4
bottom plate as wiring counted the MIM's own capacitance a second time
(the device already carries it), as magic does not.

Every edit is anchored on a line of the installed kpex; a line that is gone
(a new kpex release) exits 3 with the anchor named, so the gate refuses
rather than running an unpatched kpex.

kpex 0.6 (IIC-OSIC-TOOLS 2026.10) fixed both upstream: its tech file's poly
conductor is the whole of poly2 (`poly2_con`, gate included) and metal5 is
filed under Metal5. On a metal1 strap over an nfet gate, C(metal1, gate) is
0.265 fF from plain kpex 0.6.8 and 0.269 fF from patched 0.4.4 (none from
plain 0.4.4); the poly split on 0.6 is refused by kpex itself, whose tables
have no "Poly2 over Nplus" for the new gate layer. 0.6 also splits the MIM
plates off itself and, with `--blackbox true`, leaves their C to the device,
as metal5_ncap does on 0.4 (without it, 0.6 drops the MIM device from the
netlist and counts its C as wiring instead). So 0.6 runs its own deck and
tech with `--blackbox true`, and only 0.4 is patched. Which release is
installed is read from marks in both files (`release`); a deck and tech
that carry neither release's marks whole, or some of both, exit 3 too.

0.6 counts more of the gate poly than magic does. Its own channel cut
(`mos_channels`) takes the channel off the substrate and drops a gate
edge's fringe onto the substrate, but a gate poly edge still has its
sidewall C to the poly beside it and its fringe up onto metal1 and the
metals above. magic's gf180 tech has neither, because its fet types are
never the edge of a `*poly` sidewall or poly sideoverlap. (Patched 0.4
gives PolyGate none of either, the same as magic.) The C between metal1
and the gate below, area and the metal edge's fringe, is kept: magic counts
metal over allpolynonres, fets included. To compare like with like, 0.6
runs with hooks on kpex's cap results. Each Poly2 sidewall or Poly2-inside
sideoverlap is measured for the share of its edge (either side's, on a
sidewall) along a channel, and each Poly2-top overlap for the share of its
area on one. That share is written per net pair to
DECK_DIR/gate_poly_excluded.json, and the crosscheck subtracts it from
kpex's nets. kpex's own netlist is left as it is. A hook point that is
missing, or a cap with no geometry to measure, exits 3, so the share is
never quietly zero. 0.4 writes the same file with no pairs."""
import copy
import json
import os
import shutil
import sys

NONE = 1e-6  # kpex divides by a capacitance: a zero is "none" this way


def fail(msg: str) -> None:
    print(f"kpex_runner: {msg}", file=sys.stderr)
    raise SystemExit(3)


def patch(work: str, rel: str, old: str, new: str) -> None:
    path = os.path.join(work, rel)
    text = open(path, encoding="utf-8").read()
    if text.count(old) != 1:
        fail(f"{rel}: anchor {old.strip()!r} found {text.count(old)} times, "
             "not once - the installed kpex changed its GF180 deck")
    open(path, "w", encoding="utf-8").write(text.replace(old, new))


# the GF180 deck's and tech file's marks of each kpex release: 0.4 lists the
# deck's layer names in gf180mcu.lvs and calls the poly conductor poly_con,
# with metal5 under MetalTop; 0.6 names the layers itself
# (auto_name_layers!), calls it poly2_con and files metal5 under Metal5
MARKS = {
    "0.4": (("deck", " poly2_con pgate_lv_base"), ("tech", "poly_con", "Poly2"),
            ("tech", "metal5_con", "MetalTop")),
    "0.6": (("deck", "auto_name_layers!(binding)\n"),
            ("tech", "poly2_con", "Poly2"), ("tech", "metal5_con", "Metal5")),
}


def release(lvs: str, tech: dict) -> str:
    """The kpex release whose marks gf180mcu.lvs (LVS) and the tech json
    (TECH) carry: all of one release's and none of another's, else exit 3
    with what was found, so a new release is never run as an old one."""
    layers = {x["layer_info"]["name"]: x.get("original_layer_name")
              for x in tech.get("lvs_computed_layers", [])}

    def has(mark):
        if mark[0] == "deck":
            return mark[1] in lvs
        return layers.get(mark[1]) == mark[2]
    found = {r: [has(m) for m in marks] for r, marks in MARKS.items()}
    whole = [r for r, f in found.items() if all(f)]
    if len(whole) != 1 or any(any(f) for r, f in found.items()
                              if r not in whole):
        seen = "; ".join(f"{r}: {sum(f)} of {len(f)}" for r, f in found.items())
        fail(f"GF180 deck and tech carry no single kpex release's marks "
             f"({seen}) - the installed kpex changed again")
    return whole[0]


def upstream_args(args: list[str]) -> list[str]:
    """kpex 0.6's arguments: the caller's, with the MIM plates' C left to
    the device (`--blackbox true`) unless the caller chose."""
    if any(a.startswith("--blackbox") for a in args):
        return list(args)
    return [*args, "--blackbox", "true"]


def patched_tech(tech: dict) -> dict:
    t = copy.deepcopy(tech)
    gate = None
    for layer in t.get("lvs_computed_layers", []):
        if layer["layer_info"]["name"] == "poly_con":
            if gate is not None:
                fail("tech: two lvs_computed_layers named poly_con")
            layer["layer_info"]["name"] = "poly2_wire"
            gate = copy.deepcopy(layer)
            gate["layer_info"]["name"] = "poly2_gate"
            gate["layer_info"]["drw_gds_pair"] = {"layer": 30, "datatype": 77}
            gate["original_layer_name"] = "PolyGate"
    if gate is None:
        fail("tech: no lvs_computed_layer named poly_con")
    t["lvs_computed_layers"].append(gate)
    # metal5_con (81/0) is filed under "MetalTop", a name the capacitance
    # tables give only a substrate entry: a metal5 shape near any other
    # conductor then dies on a KeyError in the sidewall/fringe pass. The
    # process stack and every table call the layer Metal5.
    m5 = [x for x in t["lvs_computed_layers"]
          if x["layer_info"]["name"] == "metal5_con"]
    if len(m5) != 1 or m5[0]["original_layer_name"] not in ("MetalTop",
                                                               "Metal5"):
        fail("tech: no single lvs_computed_layer metal5_con on MetalTop")
    m5[0]["original_layer_name"] = "Metal5"
    # the cap layer is metal5 off the MIM top plate (metal5_ncap): over
    # fusetop it is the plate's own strap, and its area over the metal4
    # bottom plate is the MIM device's capacitance, not wiring
    m5[0]["layer_info"]["name"] = "metal5_ncap"
    t["layers"].append({"purpose": "PURPOSE_METAL", "name": "PolyGate",
                        "description": "gate poly over a channel",
                        "drw_gds_pair": {"layer": 30, "datatype": 77}})
    stack = t["process_stack"]["layers"]
    names = [x["name"] for x in stack]
    if "Poly2" not in names:
        fail("tech: no Poly2 in the process stack")
    i = names.index("Poly2")
    poly_gate = copy.deepcopy(stack[i])
    poly_gate["name"] = "PolyGate"
    poly_gate.get("metal_layer", {}).pop("contact_above", None)
    # below Poly2: kpex shields a Poly2 edge's fringe to the substrate only
    # with the layers between the two, so the gate it ends on has to be one
    # of them. Above it, the wire/gate seam (an edge drawn by this split,
    # not one in the layout) fringed to the substrate as if open, over the
    # channel the gate covers: 2 fF on a pair of L=10um gates that magic,
    # which counts no poly perimeter onto a fet, does not have.
    stack.insert(i, poly_gate)

    c = t["process_parasitics"]["capacitance"]
    over = [o for o in c["overlaps"] if o["bottom_layer_name"] == "Poly2"]
    fringe = [o for o in c["sideoverlaps"] if o["out_layer_name"] == "Poly2"]
    if not over or not fringe:
        fail("tech: no overlap or sideoverlap entry onto Poly2")
    # a metal over the gate: as over wire poly
    c["overlaps"] += [dict(o, bottom_layer_name="PolyGate") for o in over]
    c["sideoverlaps"] += [dict(o, out_layer_name="PolyGate") for o in fringe]
    # the gate's own C (to the substrate, from its edges, onto layers above
    # from its edges): none, as in magic
    c["substrates"].append({"layer_name": "PolyGate", "area_capacitance": NONE,
                            "perimeter_capacitance": NONE})
    c["sidewalls"] += [dict(o, layer_name="PolyGate", capacitance=NONE)
                       for o in c["sidewalls"] if o["layer_name"] == "Poly2"]
    c["overlaps"] += [dict(o, top_layer_name="PolyGate", capacitance=NONE)
                      for o in c["overlaps"] if o["top_layer_name"] == "Poly2"]
    c["sideoverlaps"] += [dict(o, in_layer_name="PolyGate", capacitance=NONE)
                          for o in c["sideoverlaps"]
                          if o["in_layer_name"] == "Poly2"]
    for a, b in (("PolyGate", "Poly2"), ("Poly2", "PolyGate")):
        c["overlaps"].append({"top_layer_name": a, "bottom_layer_name": b,
                              "capacitance": NONE})
        c["sideoverlaps"].append({"in_layer_name": a, "out_layer_name": b,
                                  "capacitance": NONE})
    return t


EXCLUDED = "gate_poly_excluded.json"
GATE_LAYER = "Poly2"
RULE = ("Poly2 sidewall and Poly2-inside sideoverlap along a MOS channel, "
        "Poly2-top overlap on one (magic's gf180 fets: no *poly sidewall or "
        "poly fringe)")


def _edge_spans(edge, channels, kdb) -> list[tuple[float, float]]:
    """The parts of EDGE along CHANNELS (within 1 DBU), as fractions of its
    length from p1."""
    n = edge.length()
    if n == 0:
        return []
    d = edge.d()
    out = []
    for part in kdb.Edges([edge]).inside_part(channels).each():
        t = sorted(((p.x - edge.p1.x) * d.x + (p.y - edge.p1.y) * d.y) / (n * n)
                   for p in (part.p1, part.p2))
        out.append((max(0.0, t[0]), min(1.0, t[1])))
    return out


def _union_share(spans: list[tuple[float, float]]) -> float:
    got, end = 0.0, 0.0
    for a, b in sorted(spans):
        a = max(a, end)
        if b > a:
            got += b - a
            end = b
    return min(got, 1.0)


class GatePolyShare:
    """Hooks on kpex 0.6's cap results that measure the gate-poly C magic
    leaves out (module docstring). Errors go to `errors`, not out of the
    hook, since kpex runs some of these from KLayout's C++ callbacks."""

    def __init__(self):
        self.channels = None
        self.cells = 0
        self.errors: list[str] = []
        self.pairs: dict[tuple[str, str], dict[str, float]] = {}

    def add(self, a: str, b: str, term: str, ff: float) -> None:
        if ff <= 0:
            return
        key = tuple(sorted((a, b)))
        terms = self.pairs.setdefault(key, {})
        terms[term] = terms.get(term, 0.0) + ff

    def install(self) -> None:
        try:
            from klayout_pex.rcx25 import extraction_results as er
            from klayout_pex.rcx25.extractor import RCX25Extractor
            import klayout.db as kdb
        except ImportError as exc:
            fail(f"kpex 0.6 hook: {exc}")
        want = [(er.CellExtractionResults, m) for m in (
            "add_sidewall_cap", "add_sideoverlap_cap", "add_overlap_cap")]
        want.append((RCX25Extractor, "extract_cell"))
        for cls, name in want:
            if not callable(getattr(cls, name, None)):
                fail(f"kpex 0.6 hook: {cls.__name__}.{name} is gone - the "
                     "installed kpex changed, so the gate-poly share magic "
                     "leaves out cannot be measured")
        share = self

        orig_cell = RCX25Extractor.extract_cell

        def extract_cell(self, *a, **k):
            ch = getattr(getattr(self, "pex_context", None), "mos_channels",
                         None)
            if not isinstance(ch, kdb.Region):
                share.errors.append("pex_context.mos_channels is not a "
                                    "Region")
            else:
                share.channels = ch.merged().sized(1)
            share.cells += 1
            return orig_cell(self, *a, **k)
        RCX25Extractor.extract_cell = extract_cell

        def edges_share(cap, *edges) -> float | None:
            if share.channels is None:
                share.errors.append("a cap came before the channels were set")
                return None
            if any(e is None for e in edges):
                share.errors.append(f"{type(cap).__name__} {cap.key} has no "
                                    "edge to measure")
                return None
            spans = []
            for e in edges:
                spans += _edge_spans(e, share.channels, kdb)
            return _union_share(spans)

        def hook(name, measure):
            orig = getattr(er.CellExtractionResults, name)

            def f(self, cap, *a, **k):
                try:
                    measure(cap)
                except Exception as exc:  # noqa: BLE001 - reported after kpex
                    share.errors.append(f"{name}: {exc!r}")
                return orig(self, cap, *a, **k)
            setattr(er.CellExtractionResults, name, f)

        def sidewall(cap):
            k = cap.key
            if k.layer != GATE_LAYER:
                return
            # either side on a channel: magic's fets are neither edge
            frac = edges_share(cap, cap.inside_edge, cap.outside_edge)
            if frac:
                share.add(k.net1, k.net2, f"sidewall {k.layer}",
                          cap.cap_value * frac)

        def sideoverlap(cap):
            k = cap.key
            if k.layer_inside != GATE_LAYER:
                return
            frac = edges_share(cap, cap.inside_edge)
            if frac:
                share.add(k.net_inside, k.net_outside,
                          f"sideoverlap {k.layer_inside}>{k.layer_outside}",
                          cap.cap_value * frac)

        def overlap(cap):
            k = cap.key
            if k.layer_top != GATE_LAYER:
                return
            if share.channels is None or cap.area is None:
                share.errors.append(f"OverlapCap {k} has no area to measure")
                return
            whole = cap.area.area()
            if whole:
                frac = (cap.area & share.channels).area() / whole
                share.add(k.net_top, k.net_bot,
                          f"overlap {k.layer_top}>{k.layer_bot}",
                          cap.cap_value * min(frac, 1.0))

        hook("add_sidewall_cap", sidewall)
        hook("add_sideoverlap_cap", sideoverlap)
        hook("add_overlap_cap", overlap)

    def report(self, rel: str) -> dict:
        pairs = []
        for (a, b), terms in sorted(self.pairs.items()):
            pairs.append({"nets": [a, b], "ff": round(sum(terms.values()), 6),
                          "terms": {t: round(v, 6)
                                    for t, v in sorted(terms.items())}})
        return {"release": rel, "gate_layer": GATE_LAYER, "rule": RULE,
                "pairs": pairs}


def write_excluded(work: str, data: dict) -> None:
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(work, EXCLUDED), "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)


def main() -> int:
    if len(sys.argv) < 2:
        fail("usage: kpex_runner.py DECK_DIR <kpex arguments...>")
    work = os.path.abspath(sys.argv[1])
    try:
        from klayout_pex.pdk_config import PDK
    except ImportError as exc:
        fail(f"klayout_pex is not importable in this toolchain: {exc}")
    cfg = PDK.GF180MCUD.config
    with open(cfg.pex_lvs_script_path, encoding="utf-8") as f:
        lvs = f.read()
    with open(cfg.tech_pb_json_path, encoding="utf-8") as f:
        tech = json.load(f)
    from klayout_pex.__main__ import main as kpex_main
    if release(lvs, tech) == "0.6":
        # upstream counts gate poly and files metal5 itself (docstring)
        sys.argv = ["kpex", *upstream_args(sys.argv[2:])]
        shutil.rmtree(work, ignore_errors=True)
        share = GatePolyShare()
        share.install()
        rc = kpex_main()
        if share.errors:
            fail("kpex 0.6 hook: " + "; ".join(share.errors[:5]))
        if rc in (0, None) and share.cells == 0:
            fail("kpex 0.6 hook: extract_cell never ran, so the gate-poly "
                 "share magic leaves out was not measured")
        write_excluded(work, share.report("0.6"))
        return rc
    shutil.rmtree(work, ignore_errors=True)
    shutil.copytree(os.path.dirname(cfg.pex_lvs_script_path), work)
    patch(work, "rule_decks/general_derivations.lvs",
          "poly2_con       = poly2.not(res_mk).not(plfuse)\n",
          "poly2_con       = poly2.not(res_mk).not(plfuse)\n"
          "poly2_wire      = poly2_con.not(tgate)\n"
          "poly2_gate      = poly2_con.and(tgate)\n")
    patch(work, "rule_decks/general_connections.lvs",
          "connect(poly2_con, contact)\n",
          "connect(poly2_con, contact)\nconnect(poly2_con, poly2_wire)\n"
          "connect(poly2_con, poly2_gate)\n")
    patch(work, "gf180mcu.lvs", " poly2_con pgate_lv_base",
          " poly2_con poly2_wire poly2_gate pgate_lv_base")
    patch(work, "rule_decks/general_derivations.lvs",
          "  metal5_con = metal5.not(metal5_res)\n",
          "  metal5_con = metal5.not(metal5_res)\n"
          "  metal5_ncap = metal5_con.not(fusetop)\n")
    patch(work, "rule_decks/general_connections.lvs",
          "  connect(via4_n_cap, metal5_con)\n",
          "  connect(via4_n_cap, metal5_con)\n"
          "  connect(metal5_con, metal5_ncap)\n")
    patch(work, "gf180mcu.lvs", " metal5_con via4_n_cap",
          " metal5_con metal5_ncap via4_n_cap")
    tech = patched_tech(tech)
    tech_path = os.path.join(work, "tech.pb.json")
    with open(tech_path, "w", encoding="utf-8") as f:
        json.dump(tech, f, indent=1)
    cfg.tech_pb_json_path = tech_path
    cfg.pex_lvs_script_path = os.path.join(work, "gf180mcu.lvs")

    # the patched tech already gives the gate poly none of what magic's
    # leaves out
    write_excluded(work, {"release": "0.4", "gate_layer": "PolyGate",
                          "rule": "none: the patched tech's PolyGate",
                          "pairs": []})
    sys.argv = ["kpex", *sys.argv[2:]]
    return kpex_main()


if __name__ == "__main__":
    raise SystemExit(main())
