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
rather than running an unpatched kpex."""
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


def main() -> int:
    if len(sys.argv) < 2:
        fail("usage: kpex_runner.py DECK_DIR <kpex arguments...>")
    work = os.path.abspath(sys.argv[1])
    try:
        from klayout_pex.pdk_config import PDK
    except ImportError as exc:
        fail(f"klayout_pex is not importable in this toolchain: {exc}")
    cfg = PDK.GF180MCUD.config
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
    with open(cfg.tech_pb_json_path, encoding="utf-8") as f:
        tech = patched_tech(json.load(f))
    tech_path = os.path.join(work, "tech.pb.json")
    with open(tech_path, "w", encoding="utf-8") as f:
        json.dump(tech, f, indent=1)
    cfg.tech_pb_json_path = tech_path
    cfg.pex_lvs_script_path = os.path.join(work, "gf180mcu.lvs")

    from klayout_pex.__main__ import main as kpex_main
    sys.argv = ["kpex", *sys.argv[2:]]
    return kpex_main()


if __name__ == "__main__":
    raise SystemExit(main())
