#!/usr/bin/env python
"""check_analog_erc.py - the ade `erc` gate: reliability ERC with CVC on the
magic-extracted layout (GitHub issue 122).

    check_analog_erc.py --workspace DIR [--block NAME] [--schematic]
                        [--out FILE]

gates.yaml's ade.erc row names this tool `analog_erc`, so it never collides
with a digital `erc` script. Like `lvs`, it regenerates the GDS from
`layout/gen_<block>.py` (layout_gen.build) and extracts it with magic. It
never reads a GDS or netlist already on disk.

What it checks. CVC (cvc_rv, Circuit Validation Check) runs on the extracted
netlist. Each pin gets a voltage: `vdd` is spec.yaml's supply.vdd, `vss`,
`gnd` and SPICE's global `0` are 0 V, and every other pin swings from 0 V to
supply.vdd. spec.yaml's optional `erc_nets` overrides any pin (speclib).
CVC then propagates the min and max voltage of every net and checks each
device against its class's limits (MODELS below):
  - gate oxide: Vgs and Vbg over the device's rating
    (overvoltage_vgs, overvoltage_vbg);
  - junctions: Vds and Vbs over the rating (overvoltage_vds, overvoltage_vbs);
  - well and bulk ties: an NMOS whose bulk can sit above its source or
    drain, or a PMOS whose bulk can sit below them, e.g. a PMOS whose nwell
    tap is wired to vss (bulk_tie);
  - floating gates: a gate on a net that nothing drives (floating_gate);
  - diodes that can be forward biased (forward_bias_diode);
  - min/max voltage conflicts, fuse paths and model errors;
  - bipolar transistors (the Q class, below).
Those are errors. CVC's leak and gate-vs-source logic checks come back as
warnings, with CVC's own words. They flag paths a design can mean, such as a
resistor ladder driven from its inputs.

The Q class. CVC has no BJT checks of its own, so each GF180 vertical PNP
(collector = p-substrate, base = nwell, emitter = P+) and NPN (with its
own substrate terminal) becomes one CVC diode per junction (JUNCTIONS),
each rated at the 3.3 V LV class limit (the PDK puts every BJT in the LV
set, junction breakdowns 7 V and up, a PNP's BVCEO 10 V): a junction over
it is overvoltage_junction. The junction that is the substrate (a PNP's
collector to base, an NPN's substrate to collector) is also checked for
forward bias: a substrate that can rise above the well is substrate_tie.
The emitter-base junction also gets a 1 kohm resistor, its real DC path,
so a self-biased core that reaches its supply through a PNP (a bandgap)
is not reported as floating gates. And a BJT whose substrate terminal is
not on a 0 V supply pin is substrate_not_ground: GF180's p-substrate is
one net, the chip's ground. magic names an extracted PNP by the first
pnp rule in the PDK's tech file, whatever its emitter size; every PNP has
the same rating, so the check holds, but a finding's model can name the
wrong PNP size.

`--schematic` runs the same check on netlist/<block>.cir (its `.subckt`
defaults and `.param`s substituted) for a block with no layout yet, e.g. a
bandgap, its findings filed against the .cir and its CVC files under
log/erc_schematic. A finding there names the schematic device.

Ratings are GF180MCU's own operating classes, each limit the rating plus
10% (3.3 V devices 3.63 V, 5 V 5.5 V, 6 V 6.6 V; the 10 V asymmetric
devices 11 V drain to source with a 6 V gate). A device class not in
MODELS is a refusal: the gate does not guess a rating.

Each finding names the extracted device (X3), its schematic refdes when one
device in netlist/*.cir has the same model, nets and size, the terminal or
terminals involved with each one's net and voltage range, and the device's
position in um in the layout (read from magic's .ext), the way the lvs gate
locates a device. Fingers of one transistor with the same finding are one
finding: the first finger names it and `parallel` lists the others.

Waivers: spec/erc_waivers.yaml (load_waivers below). One entry per finding,
with the device, its model, the finding kind and all its nets, plus a
reason and an owner. A matched finding stays in the report as an `info`
with the waiver attached. An entry that matches nothing is itself an error,
so a stale waiver never outlives its device. Prefer fixing the design: no
corpus design carries a waiver.

Refusals (exit 2), never a pass: no layout generator, a device class with
no rating, a model or power file CVC would not load (cvc_rv exits 0 even
then, so its log is read), no error-count summary, or a malformed
erc_nets or waiver file. A nonzero CVC count that the error report does not
explain becomes an `erc_unparsed` error. Nothing CVC counted is dropped.

Not checked: a resistor's third (body) terminal. CVC's resistor has two, so
a poly or diffusion resistor's body diode is not modelled. A BJT's own
current, beta or saturation: CVC sees voltages, not the transistor. A device on an
unrated class refuses rather than passing.
"""
from __future__ import annotations

import argparse
import gzip
import re
import sys
from pathlib import Path

import yaml

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))
import checklib  # noqa: E402
import layoutlib  # noqa: E402
import layout_gen  # noqa: E402
import speclib  # noqa: E402
from checklib import CheckError  # noqa: E402

SCRIPT = "check_analog_erc"
TOOL = "analog_erc"
SPEC_REL = "spec/spec.yaml"
WAIVERS_REL = "spec/erc_waivers.yaml"
GROUND_PINS = ("vss", "gnd")

# GF180MCU device classes: (CVC type, Vth, Vgs/Vbg limit, Vds/Vbs limit).
_LV, _MV, _HV = 3.63, 5.5, 6.6
MOS = {
    "nfet_03v3": ("N", 0.6, _LV, _LV), "pfet_03v3": ("P", -0.6, _LV, _LV),
    "nfet_03v3_dss": ("N", 0.6, _LV, _LV), "pfet_03v3_dss": ("P", -0.6, _LV, _LV),
    "nfet_05v0": ("N", 0.7, _MV, _MV), "pfet_05v0": ("P", -0.7, _MV, _MV),
    "nfet_06v0": ("N", 0.7, _HV, _HV), "pfet_06v0": ("P", -0.7, _HV, _HV),
    "nfet_06v0_dss": ("N", 0.7, _HV, _HV), "pfet_06v0_dss": ("P", -0.7, _HV, _HV),
    "nfet_06v0_nvt": ("N", 0.1, _HV, _HV),
    "nfet_10v0_asym": ("N", 0.7, _HV, 11.0), "pfet_10v0_asym": ("P", -0.7, _HV, 11.0),
}
# Resistors: sheet ohms/square (sm141064.spice's typical rsh_*). CVC only
# needs a resistance to rank paths; the values are the PDK's own.
RES = {
    "rm1": 0.09, "rm2": 0.09, "rm3": 0.09, "tm6k": 0.06, "tm9k": 0.04,
    "tm11k": 0.04, "tm30k": 0.01,
    "nplus_u": 85.0, "pplus_u": 128.0, "nplus_s": 6.3, "pplus_s": 7.0,
    "npolyf_u": 310.0, "ppolyf_u": 350.0, "npolyf_s": 6.8, "ppolyf_s": 7.3,
    "ppolyf_u_1k": 1000.0, "ppolyf_u_1k_6p0": 1000.0, "ppolyf_u_2k": 2000.0,
    "ppolyf_u_2k_6p0": 2000.0, "ppolyf_u_3k": 3000.0, "ppolyf_u_3k_6p0": 3000.0,
    "nwell": 1000.0,
}
DIODES = ("diode_nd2ps_03v3", "diode_pd2nw_03v3", "diode_nd2ps_06v0",
          "diode_pd2nw_06v0", "diode_nw2ps_03v3", "diode_nw2ps_06v0",
          "diode_pw2dw", "diode_dw2ps", "sc_diode")
CAPS = ("cap_mim_1f5fF", "cap_mim_1f0fF", "cap_mim_2f0fF",
        "cap_mim_2f0_m4m5_noshield", "cap_mim_1f5_m4m5_noshield",
        "cap_mim_1f0_m4m5_noshield")
# Bipolars: GF180MCU's vertical PNP (collector = p-substrate, base = nwell,
# emitter = P+) and its deep-nwell NPN (collector = deep nwell, base =
# pwell, emitter = N+, substrate = p-substrate). The PDK lists both in its
# 3.3 V LV device set (the VPNP as "(3.3V LV)", BVCEO -10 V; each junction's
# breakdown is above 7 V), so every junction and C-E are rated at the LV
# class limit. magic names the 10x10 PNP pnp_10p00x00p42 (its device
# parameters match no other extraction model), so that name is here too.
BJT = {"pnp_05p00x00p42": "pnp", "pnp_05p00x05p00": "pnp",
       "pnp_10p00x00p42": "pnp", "pnp_10p00x10p00": "pnp",
       "npn_00p54x02p00": "npn", "npn_00p54x04p00": "npn",
       "npn_00p54x08p00": "npn", "npn_00p54x16p00": "npn",
       "npn_05p00x05p00": "npn", "npn_10p00x10p00": "npn"}
BJT_LIMIT = _LV
BJT_PINS = {"pnp": "cbe", "npn": "cbes"}   # ext2spice's (and sm141064's) order
# Each BJT goes to CVC as one diode per junction, (anode, cathode, checked
# for forward bias). Emitter-base and NPN base-collector junctions conduct
# in normal use, so they are only rated; the substrate junction (PNP
# collector-base, NPN substrate-collector) must never conduct, so it is
# rated and checked: that is the substrate tie.
JUNCTIONS = {
    "pnp": {"eb": ("e", "b", False), "ce": ("c", "e", False),
            "cb": ("c", "b", True)},
    "npn": {"be": ("b", "e", False), "bc": ("b", "c", False),
            "ce": ("c", "e", False), "sc": ("s", "c", True)},
}
SUBSTRATE = {"pnp": "c", "npn": "s"}
# The emitter-base junction also goes to CVC as a resistor (CONDUCT_OHMS):
# it is forward biased in use and holds the emitter one Vbe from the base,
# a DC path CVC's diode does not carry. Without it a bandgap's self-biased
# core (a diode-connected NMOS whose source is a PNP emitter) has no path
# to a supply and its bias gates read as floating.
CONDUCTS = {"pnp": "eb", "npn": "be"}
CONDUCT_OHMS = 1000.0
BJT_TERMINALS = {"c": "collector", "b": "base", "e": "emitter",
                 "s": "substrate"}

# CVC's report sections -> (finding kind, severity, its summary count line).
SECTIONS = [
    ("forward bias diode", "forward_bias_diode", "error", "Forward Bias Diodes"),
    ("nmos source/drain vs bias", "bulk_tie", "error", "NMOS Source vs Bulk"),
    ("pmos source/drain vs bias", "bulk_tie", "error", "PMOS Source vs Bulk"),
    ("nmos gate vs source", "gate_vs_source", "warning", "NMOS Gate vs Source"),
    ("pmos gate vs source", "gate_vs_source", "warning", "PMOS Gate vs Source"),
    ("vbg overvoltage", "overvoltage_vbg", "error", "Overvoltage-VBG"),
    ("vbs overvoltage", "overvoltage_vbs", "error", "Overvoltage-VBS"),
    ("vds overvoltage", "overvoltage_vds", "error", "Overvoltage-VDS"),
    ("vgs overvoltage", "overvoltage_vgs", "error", "Overvoltage-VGS"),
    ("model errors", "model_error", "error", "Model errors"),
    ("nmos possible leak", "possible_leak", "warning", "NMOS Possible Leaks"),
    ("pmos possible leak", "possible_leak", "warning", "PMOS Possible Leaks"),
    ("mos floating input", "floating_gate", "error", "HI-Z Inputs"),
    ("expected values", "unexpected_voltage", "error", "Unexpected voltage"),
    ("ldd errors", "ldd", "warning", "LDD drain->source"),
    ("voltage already set", "voltage_conflict", "error", "Voltage Conflicts"),
    ("path through fuse", "fuse", "error", "Fuse Problems"),
    ("switch shorts", "leak", "warning", "Leaks"),
    ("logic shorts", "leak", "warning", "Leaks"),
    ("resistor errors", "leak", "warning", "Leaks"),
]
# summary lines whose counts fold into one SECTIONS count
COUNT_ALIASES = {"Min Voltage Conflicts": "Voltage Conflicts",
                 "Max Voltage Conflicts": "Voltage Conflicts"}
TERMINALS = {"G": "gate", "S": "source", "D": "drain", "B": "bulk"}
DIODE_TERMINALS = {"S": "anode", "D": "cathode"}
RES_TERMINALS = {"S": "a", "D": "b"}
WHAT = {
    "overvoltage_vgs": "gate oxide: gate to source/drain over the rating",
    "overvoltage_vbg": "gate oxide: gate to bulk over the rating",
    "overvoltage_vds": "drain to source over the rating",
    "overvoltage_vbs": "bulk to source/drain over the rating",
    "bulk_tie": "bulk can forward-bias against source/drain: a well or "
                "substrate tie on the wrong net",
    "floating_gate": "gate on a net nothing drives",
    "forward_bias_diode": "diode can be forward biased",
    "overvoltage_junction": "bipolar junction over the rating",
    "substrate_tie": "substrate junction can forward-bias: the substrate "
                     "terminal must be the lowest net",
}
# CVC's diode findings on a BJT junction, as the BJT's own finding kinds
BJT_KINDS = {"forward_bias_diode": "substrate_tie",
             "overvoltage_vds": "overvoltage_junction"}
REFUSE_PATTERNS = ("Invalid model", "No model match", "skipped due to problems",
                   "unexpected error", "could not expand signal")


def device_class(model: str) -> str | None:
    m = model.lower()
    if m in MOS:
        return "mos"
    if m in RES:
        return "res"
    if m in DIODES:
        return "diode"
    if m in CAPS:
        return "cap"
    if m in BJT:
        return "bjt"
    return None


def _params(tokens: list[str]) -> dict[str, str]:
    return {k.lower(): v for k, _, v in (t.partition("=") for t in tokens)}


def _um(v: str | None) -> float | None:
    """A SPICE length (8u, 5e-7, 0.5u) in um."""
    if v is None:
        return None
    m = re.fullmatch(r"([-+0-9.eE]+)([a-zA-Z]*)", v.strip())
    if not m:
        return None
    scale = {"": 1e6, "u": 1.0, "n": 1e-3, "m": 1e3, "p": 1e-6}.get(
        m.group(2).lower()[:1])
    return None if scale is None else round(float(m.group(1)) * scale, 4)


def to_cdl(spice: str, topcell: str):
    """magic's flat ext2spice netlist as a CDL CVC reads, plus what each CVC
    device is: ({cvc name: {name, model, class, nets, w, l}}, ports, cdl).

    ext2spice writes every PDK device as a subcircuit call (`X0 d g s b
    nfet_03v3 ... w=8u l=0.5u`); CVC matches models on the CDL letter, so
    a MOS becomes `M0`, a resistor `R0` (two terminals, `w`/`l` for CVC's
    `R=l/w*rsh`), a MIM cap `C0`, a diode `D0`. A BJT (`X0 c b e [s]
    pnp_...`) becomes one diode per junction, `DQ0_eb` and so on, each on
    its own CVC model `<model>_<junction>` (JUNCTIONS). Any other device
    class refuses."""
    lines = []
    for raw in spice.splitlines():
        if raw.startswith("+") and lines:
            lines[-1] += " " + raw[1:]
        else:
            lines.append(raw)
    out, devices, ports, in_top = [], {}, None, False
    for line in lines:
        t = line.split()
        if not t or t[0].startswith("*"):
            continue
        head = t[0].lower()
        if head == ".subckt":
            if t[1].lower() != topcell.lower() or ports is not None:
                raise CheckError(f"extracted netlist has subckt {t[1]!r}; "
                                 f"expected one flat {topcell!r}")
            ports, in_top = t[2:], True
            out.append(line)
            continue
        if head in (".ends", ".end"):
            if head == ".ends":
                out.append(".ends")
            in_top = False
            continue
        if not in_top:
            raise CheckError(f"extracted netlist line outside the top cell: "
                             f"{line.strip()!r}")
        if head[0] not in "xd":
            raise CheckError(f"extracted netlist has a device CVC is not "
                             f"set up for: {line.strip()!r}")
        first_param = next((i for i, tok in enumerate(t) if "=" in tok), len(t))
        model = t[first_param - 1]
        nets = t[1:first_param - 1]
        params = _params(t[first_param:])
        cls = device_class(model)
        num = re.sub(r"^[xXdD]", "", t[0])
        if cls == "bjt" and len(nets) == len(BJT_PINS[BJT[model.lower()]]):
            kind = BJT[model.lower()]
            terms = dict(zip(BJT_PINS[kind], nets))
            for j, (a, c, _fwd) in JUNCTIONS[kind].items():
                name = f"DQ{num}_{j}"
                if name in devices:
                    raise CheckError(f"two extracted devices map to CVC name "
                                     f"{name}")
                out.append(f"{name} {terms[a]} {terms[c]} {model}_{j}")
                devices[name] = {"name": t[0], "model": model, "class": "bjt",
                                 "junction": j, "cvc_model": f"{model}_{j}",
                                 "nets": terms, "w": None, "l": None}
            j = CONDUCTS[kind]
            a, c, _fwd = JUNCTIONS[kind][j]
            name = f"RQ{num}_{j}"
            out.append(f"{name} {terms[a]} {terms[c]} {model}_on w=1u l=1u")
            devices[name] = {"name": t[0], "model": model, "class": "bjt",
                             "junction": j, "cvc_model": f"{model}_on",
                             "path": True, "nets": terms, "w": None, "l": None}
            continue
        if cls == "mos" and len(nets) == 4:
            name = f"M{num}"
            w, length = _um(params.get("w")), _um(params.get("l"))
            out.append(f"{name} {' '.join(nets)} {model} w={params.get('w')} "
                       f"l={params.get('l')}")
            terms = dict(zip("dgsb", nets))
        elif cls == "res" and len(nets) in (2, 3):
            name = f"R{num}"
            w = _um(params.get("r_width", params.get("w")))
            length = _um(params.get("r_length", params.get("l")))
            if not w or not length:
                raise CheckError(f"resistor {t[0]} has no width/length: "
                                 f"{line.strip()!r}")
            out.append(f"{name} {nets[0]} {nets[1]} {model} w={w}u l={length}u")
            terms = dict(zip("ab", nets[:2]))
        elif cls == "cap" and len(nets) == 2:
            name, w, length = f"C{num}", None, None
            out.append(f"{name} {nets[0]} {nets[1]} {model}")
            terms = dict(zip("ab", nets))
        elif cls == "diode" and len(nets) == 2:
            name, w, length = f"D{num}", None, None
            out.append(f"{name} {nets[0]} {nets[1]} {model}")
            terms = dict(zip("ac", nets))
        else:
            raise CheckError(
                f"device {t[0]} is a {model!r} with {len(nets)} terminals, a "
                "class this gate has no voltage rating for - add it to "
                "check_analog_erc.py's MODELS tables with the PDK's rating")
        if name in devices:
            raise CheckError(f"two extracted devices map to CVC name {name}")
        devices[name] = {"name": t[0], "model": model, "class": cls,
                         "nets": terms, "w": w, "l": length}
    if ports is None:
        raise CheckError(f"extracted netlist has no .subckt {topcell}")
    return devices, ports, "\n".join(out) + "\n"


def model_file(devices: dict) -> str:
    lines = []
    for model in sorted({d.get("cvc_model", d["model"])
                         for d in devices.values()}):
        cls = device_class(model)
        base, _, j = model.rpartition("_")
        if cls is None and device_class(base) == "bjt" and j == "on":
            lines.append(f"R {model} R=l/w*{CONDUCT_OHMS:g}")
        elif cls is None and device_class(base) == "bjt":
            # a forward-checked junction keeps CVC's default diode (anode
            # terminal 1, cathode 2); a rated-only one gets a degenerate
            # 1-1 diode, which CVC never sees forward biased. Vds on a
            # diode is |anode - cathode|, either way round.
            fwd = JUNCTIONS[BJT[base.lower()]][j][2]
            lines.append(f"D {model}{'' if fwd else ' diode=(1-1)'} "
                         f"Vds={BJT_LIMIT}")
        elif cls == "mos":
            typ, vth, vg, vd = MOS[model.lower()]
            lines.append(f"M{typ} {model} Vth={vth} Vgs={vg} Vbg={vg} "
                         f"Vds={vd} Vbs={vd}")
        elif cls == "res":
            lines.append(f"R {model} R=l/w*{RES[model.lower()]}")
        elif cls == "diode":
            lines.append(f"D {model}")
        elif cls == "cap":
            lines.append(f"C {model}")
    return "\n".join(lines) + "\n"


def pin_voltages(ports: list[str], has_ground0: bool, vdd: float,
                 overrides: dict) -> dict[str, dict]:
    """Each pin's ERC voltage: {"power": V} or {"min": V, "max": V}."""
    lower = {p.lower(): p for p in ports}
    unknown = sorted(set(overrides) - set(ports))
    if unknown:
        raise CheckError(f"{SPEC_REL} erc_nets names {unknown}, which are not "
                         f"pins of the extracted cell (pins: {ports})")
    out = {}
    for p in ports:
        if p in overrides:
            out[p] = overrides[p]
        elif p.lower() == "vdd":
            out[p] = {"power": vdd}
        elif p.lower() in GROUND_PINS or p == "0":
            out[p] = {"power": 0.0}
        else:
            out[p] = {"min": 0.0, "max": vdd}
    if has_ground0 and "0" not in lower:
        out["/0"] = {"power": 0.0}
    return out


def power_file(pins: dict[str, dict]) -> str:
    lines = []
    for net, v in pins.items():
        if "power" in v:
            lines.append(f"{net} power {v['power']:g}")
        else:
            lines.append(f"{net} input min@{v['min']:g} max@{v['max']:g}")
    return "\n".join(lines) + "\n"


def run_cvc(work: Path, topcell: str, cdl: str, models: str, power: str,
            timeout: float = 300.0) -> tuple[str, str]:
    """(cvc.log, cvc.error) text. Refuses whatever CVC could not load."""
    work.mkdir(parents=True, exist_ok=True)
    for name in ("cvc.log", "cvc.error.gz"):
        (work / name).unlink(missing_ok=True)
    (work / f"{topcell}.cdl").write_text(cdl, encoding="utf-8")
    (work / "cvc.models").write_text(models, encoding="utf-8")
    (work / "cvc.power").write_text(power, encoding="utf-8")
    (work / "cvcrc").write_text(
        f"CVC_TOP = '{topcell}'\nCVC_NETLIST = '{topcell}.cdl'\n"
        "CVC_MODE = 'erc'\nCVC_MODEL_FILE = 'cvc.models'\n"
        "CVC_POWER_FILE = 'cvc.power'\nCVC_REPORT_FILE = 'cvc.log'\n"
        f"CVC_REPORT_TITLE = '{topcell} erc'\n", encoding="utf-8")
    try:
        proc = layoutlib.run_eda(["cvc_rv", "cvcrc"], cwd=work, timeout=timeout)
    except layoutlib.LayoutError as exc:
        raise CheckError(f"cvc_rv did not run: {exc}") from exc
    log_path, err_path = work / "cvc.log", work / "cvc.error.gz"
    log = log_path.read_text(encoding="utf-8", errors="replace") \
        if log_path.is_file() else ""
    said = (proc.stdout or "") + (proc.stderr or "") + log
    for pat in REFUSE_PATTERNS:
        if pat in said:
            line = next(ln for ln in said.splitlines() if pat in ln)
            raise CheckError(f"cvc_rv could not load its inputs: {line.strip()} "
                             f"(see log/erc/cvc.log)")
    if "CVC: Error Counts" not in log or not err_path.is_file():
        tail = "\n".join(said.strip().splitlines()[-15:])
        raise CheckError(f"cvc_rv wrote no error-count summary (exit "
                         f"{proc.returncode}); tail:\n{tail}")
    with gzip.open(err_path, "rt", encoding="utf-8", errors="replace") as fh:
        return log, fh.read()


def parse_counts(log: str) -> dict[str, int]:
    """CVC's `CVC: Error Counts` block, {"HI-Z Inputs": 1, ...}, Total out."""
    block = log.split("CVC: Error Counts", 1)[1]
    counts = {}
    for m in re.finditer(r"^CVC: ([^:\n]+?)\s*:\s*(\d+)\s*$", block, re.M):
        key = m.group(1).strip()
        if key == "Total":
            break
        key = COUNT_ALIASES.get(key, key)
        counts[key] = counts.get(key, 0) + int(m.group(2))
    return counts


def _volts(s: str) -> float | None:
    """A CVC voltage in V from its mV: `vss@0`, `=3300`, or `vss@0(5000)`,
    where a net reached through a resistance keeps its own calculated
    voltage in the brackets."""
    m = re.search(r"@(-?\d+(?:\.\d+)?)\((-?\d+(?:\.\d+)?)\)", s) \
        or re.search(r"[@=](-?\d+(?:\.\d+)?)(?:\s|$)", s)
    return None if m is None else int(round(float(m.group(m.lastindex)))) / 1000.0


def parse_errors(text: str) -> list[dict]:
    """Each device entry of cvc.error, in order: {section, header, device,
    model, terms: {G: {net, min, max}, ...}}. `section` is the `! Checking
    ...` (or other `! ...`) title the entry sits under."""
    entries, section, header, cur, term = [], "", None, None, None
    for line in text.splitlines():
        if line.startswith("! Short Detected"):
            header = line[2:].strip()
            continue
        if line.startswith("! "):
            cur = term = None
            if line.strip() != "! Finished":
                section = re.sub(r"^Checking\s+", "", line[2:].strip()).rstrip(": ")
            continue
        if line.startswith("INFO:") or not line.strip():
            cur = term = None if not line.strip() else cur
            continue
        m = re.match(r"^/(\S+) ([A-Z]) (\S+)", line)
        if m:
            cur = {"section": section, "header": header, "device": m.group(1),
                   "model": m.group(3), "terms": {}}
            entries.append(cur)
            header = term = None
            continue
        m = re.match(r"^([GSDBAC]): (\S+)", line)
        if m and cur is not None:
            term = m.group(1)
            cur["terms"][term] = {"net": m.group(2).lstrip("/"),
                                  "min": None, "max": None}
            continue
        m = re.match(r"^ (Min|Max): (.*)", line)
        if m and cur is not None and term:
            cur["terms"][term][m.group(1).lower()] = _volts(m.group(2))
            continue
        if cur is None and line.strip():
            header = line.strip()
    return entries


def section_kind(section: str):
    s = section.lower()
    for key, kind, sev, count in SECTIONS:
        if key in s:
            return kind, sev, count
    return None


def ext_devices(ext_text: str) -> list[dict]:
    """magic .ext device lines as {model, gate, terms, w, l, position} in um
    (`scale R C L`: one unit is L/100 um)."""
    m = re.search(r"^scale\s+\S+\s+\S+\s+(\S+)", ext_text, re.M)
    unit = float(m.group(1)) / 100.0 if m else 0.005
    out = []
    for line in ext_text.splitlines():
        if not line.startswith("device "):
            continue
        t = line.split()
        if len(t) < 7:
            continue
        cls, model = t[1], t[2]
        try:
            x0, y0 = int(t[3]), int(t[4])
        except ValueError:
            continue
        params = _params([tok for tok in t[7:] if "=" in tok and '"' not in tok])
        names = re.findall(r'"([^"]*)"', line)
        w = float(params["w"]) * unit if "w" in params else None
        length = float(params["l"]) * unit if "l" in params else None
        if cls == "msubckt" and len(names) >= 4:
            gate, terms = names[1], sorted(names[2:4])
        elif cls == "rsubckt" and len(names) >= 4:
            gate, terms = None, sorted(names[2:4])
        else:
            gate, terms = None, sorted(names[1:])
        out.append({"model": model, "gate": gate, "terms": terms,
                    "names": sorted(set(names)),
                    "w": None if w is None else round(w, 4),
                    "l": None if length is None else round(length, 4),
                    "position": [round(x0 * unit, 3), round(y0 * unit, 3)]})
    return out


def _signature(dev: dict):
    n = dev["nets"]
    if dev["class"] == "bjt":
        return (dev["model"], dev["junction"] + ("_on" if dev.get("path")
                                                 else ""),
                tuple(sorted(n.values())), None, None)
    if dev["class"] == "mos":
        return (dev["model"], n["g"], tuple(sorted((n["s"], n["d"]))),
                dev["w"], dev["l"])
    return (dev["model"], None, tuple(sorted(n.values())), dev["w"], dev["l"])


def locate(dev: dict, ext: list[dict]) -> list[list[float]]:
    if dev["class"] == "bjt":
        nets = sorted(set(dev["nets"].values()))
        return [e["position"] for e in ext if e["model"].lower()
                == dev["model"].lower() and e.get("names") == nets]
    sig = _signature(dev)
    return [e["position"] for e in ext
            if (e["model"], e["gate"], tuple(e["terms"]), e["w"], e["l"]) == sig]


def _unit_of(cand: dict, dev: dict) -> bool:
    """`dev` is one unit of `cand` drawn as a row of equal units (a matched
    row, #118): same model, nets and L, and cand's W a whole number of dev's."""
    if not (cand["w"] and dev["w"]) or cand["w"] <= dev["w"]:
        return False
    n = cand["w"] / dev["w"]
    return abs(n - round(n)) < 1e-6 and _signature(dict(cand, w=dev["w"])) \
        == _signature(dev)


def schematic_refdes(ws: Path, dev: dict) -> str | None:
    """The netlist/*.cir device with this one's model, nets and size, when
    exactly one matches (`x<ref> d g s b model w= l=`). With none of the
    same size, the one device it is a unit of (a matched row draws a device
    as equal units, which LVS merges back)."""
    if dev["class"] != "mos":
        return None
    hits, units = [], []
    for path in sorted((ws / "netlist").glob("*.cir")):
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            t = line.split()
            if len(t) < 6 or not t[0].lower().startswith("x") \
                    or t[5].lower() != dev["model"].lower():
                continue
            p = _params(t[6:])
            cand = {"class": "mos", "model": dev["model"],
                    "nets": dict(zip("dgsb", t[1:5])),
                    "w": _um(p.get("w")), "l": _um(p.get("l"))}
            if cand["nets"]["b"] != dev["nets"]["b"]:
                continue
            if _signature(cand) == _signature(dev):
                hits.append(t[0])
            elif _unit_of(cand, dev):
                units.append(t[0])
    if hits:
        return hits[0] if len(hits) == 1 else None
    return units[0] if len(units) == 1 else None


def load_waivers(ws: Path) -> list[dict]:
    """spec/erc_waivers.yaml, absent meaning none:

        waivers:
          - device: X3            # the extracted device the finding names
            model: pfet_03v3
            kind: bulk_tie        # the finding's kind
            nets: {d: f, g: f, s: f, b: vss}   # every terminal, as reported
            reason: "why this is safe in this design"
            owner: "who answers for it"

    Anything else is a CheckError: a waiver the gate cannot read refuses."""
    path = ws / WAIVERS_REL
    if not path.is_file():
        return []
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise CheckError(f"{WAIVERS_REL} is not YAML: {exc}") from exc
    if not isinstance(doc, dict) or set(doc) - {"waivers"} \
            or not isinstance(doc.get("waivers", []), list):
        raise CheckError(f"{WAIVERS_REL} must be {{waivers: [entry, ...]}}")
    out = []
    need = ("device", "model", "kind", "nets", "reason", "owner")
    for i, w in enumerate(doc.get("waivers") or []):
        if not isinstance(w, dict) or set(w) != set(need):
            raise CheckError(f"{WAIVERS_REL} waivers[{i}] must have exactly "
                             f"the fields {list(need)}")
        for k in ("device", "model", "kind", "reason", "owner"):
            if not isinstance(w[k], str) or not w[k].strip():
                raise CheckError(f"{WAIVERS_REL} waivers[{i}].{k} must be a "
                                 "non-empty string")
        if not isinstance(w["nets"], dict) or not w["nets"] or not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in w["nets"].items()):
            raise CheckError(f"{WAIVERS_REL} waivers[{i}].nets must map each "
                             "terminal (d, g, s, b) to its net name")
        out.append(w)
    return out


def _fmt_range(t: dict) -> str:
    lo, hi = t.get("min"), t.get("max")
    if lo is None and hi is None:
        return "floating"
    if lo == hi:
        return f"{lo:g} V"
    return f"{'?' if lo is None else f'{lo:g}'}..{'?' if hi is None else f'{hi:g}'} V"


def findings(ws: Path, topcell: str, entries: list[dict], counts: dict,
             devices: dict, ext: list[dict], waivers: list[dict],
             where: str = "layout") -> list[dict]:
    out, parsed, groups = [], {}, {}
    used = [False] * len(waivers)
    for e in entries:
        sk = section_kind(e["section"])
        if sk is None:
            kind, sev, count = "erc_unparsed", "error", None
        else:
            kind, sev, count = sk
            if e["header"] and e["header"].startswith("Short Detected"):
                # CVC writes a short before its "Logic shorts" title
                kind, sev, count = "leak", "warning", "Leaks"
                e["section"] = "shorts"
        if count:
            parsed[count] = parsed.get(count, 0) + 1
        dev = devices.get(e["device"])
        if dev and dev["class"] == "bjt":
            kind = BJT_KINDS.get(kind, kind)
        if dev:
            # fingers of one transistor (same model, nets and size, source
            # and drain either way round) are one finding that lists every
            # finger in `parallel`, not one finding each
            key = (kind, e["section"], e["header"], _signature(dev),
                   dev["nets"].get("b"),
                   tuple(sorted((v["net"], v["min"], v["max"])
                                for v in e["terms"].values())))
            if key in groups:
                groups[key]["parallel"].append(dev["name"])
                continue
        names = {"diode": DIODE_TERMINALS, "res": RES_TERMINALS,
                 "cap": RES_TERMINALS}.get(dev["class"] if dev else "", TERMINALS)
        if dev and dev["class"] == "bjt":
            a, c, _fwd = JUNCTIONS[BJT[dev["model"].lower()]][dev["junction"]]
            names = {"S": BJT_TERMINALS[a], "A": BJT_TERMINALS[a],
                     "D": BJT_TERMINALS[c], "C": BJT_TERMINALS[c]}
        terms = {names.get(k, k): {"net": v["net"], "min_v": v["min"],
                                   "max_v": v["max"]}
                 for k, v in e["terms"].items()}
        nets = dict(dev["nets"]) if dev else {}
        name = dev["name"] if dev else e["device"]
        positions = locate(dev, ext) if dev else []
        refdes = schematic_refdes(ws, dev) if dev and where == "layout" \
            else None
        model = e["model"]
        if dev and dev["class"] == "bjt":
            model = dev["model"]
            nets = {BJT_TERMINALS[k]: n for k, n in nets.items()}
        size = (f" w={dev['w']:g}u l={dev['l']:g}u"
                if dev and dev["w"] and dev["l"] else "")
        at = (f" at {positions[0]} um" if len(positions) == 1 else
              f" at one of {len(positions)} same-sized places (positions)"
              if positions else "")
        detail = "; ".join(f"{k} {v['net']} {_fmt_range(e['terms'][t])}"
                           for t, (k, v) in zip(e["terms"], terms.items()))
        what = WHAT.get(kind, (e["header"] or e["section"]))
        limit = (f" ({e['header']})" if e["header"]
                 and not e["header"].startswith("Short") else "")
        junction = (f" {'-'.join(dev['junction'].upper())} junction"
                    if dev and dev["class"] == "bjt" else "")
        msg = (f"{name}{f' ({refdes})' if refdes else ''} {model}{junction}"
               f"{size}{at}: {what}{limit} - {detail}")
        v = checklib.violation(
            TOOL, sev, where, topcell, kind,
            [name] + ([refdes] if refdes else []) + sorted(set(nets.values())),
            msg, "cvc", device=name, refdes=refdes, model=model,
            terminals=terms, nets=nets, cvc_check=e["section"],
            cvc_detail=e["header"], position=positions[0] if len(positions) == 1
            else None, positions=positions, parallel=[],
            **({"junction": dev["junction"]} if junction else {}))
        if dev:
            groups[key] = v
        out.append(v)
    for v in out:
        if v.get("parallel"):
            v["msg"] = v["items"][0]["msg"] = v["msg"].replace(
                f"{v['device']} ", f"{v['device']} (+{len(v['parallel'])} "
                "parallel) ", 1).replace(" same-sized places", " places")
        for i, w in enumerate(waivers):
            if v.get("kind") != "erc_unparsed" and w["device"] in \
                    [v["device"]] + v["parallel"] \
                    and (w["model"], w["kind"]) == (v["model"], v["kind"]) \
                    and {k.lower(): n for k, n in w["nets"].items()} == v["nets"]:
                used[i] = True
                v["severity"] = "info"
                v["waived"] = {"reason": w["reason"], "owner": w["owner"]}
                v["msg"] = v["items"][0]["msg"] = \
                    f"waived ({w['owner']}: {w['reason']}): {v['msg']}"
                break
    for key, n in counts.items():
        if n and parsed.get(key, 0) != n:
            out.append(checklib.violation(
                TOOL, "error", where, topcell, "erc_unparsed", [key],
                f"CVC counted {n} '{key}' but its error report explains "
                f"{parsed.get(key, 0)} - read log/erc/cvc.error.gz", "cvc",
                cvc_count=key, counted=n, parsed=parsed.get(key, 0)))
    for i, w in enumerate(waivers):
        if not used[i]:
            out.append(checklib.violation(
                TOOL, "error", WAIVERS_REL, topcell, "erc_waiver_unused",
                [w["device"]], f"{WAIVERS_REL} waives {w['kind']} on "
                f"{w['device']} ({w['model']}, nets {w['nets']}), which this "
                "run did not report - remove the stale waiver", "cvc"))
    return out


def substrate_findings(topcell: str, devices: dict, pins: dict,
                       where: str = "layout") -> list[dict]:
    """A BJT whose substrate terminal (PNP collector, NPN substrate) is not
    on a 0 V supply pin. GF180's p-substrate is one net, the chip's ground:
    a collector on any other net means the extraction found no substrate
    tie for it, or the schematic draws a collector the silicon cannot
    have. CVC's forward-bias check only sees the voltages; this sees the
    net."""
    out, seen = [], set()
    for dev in devices.values():
        if dev["class"] != "bjt" or dev["name"] in seen:
            continue
        seen.add(dev["name"])
        term = SUBSTRATE[BJT[dev["model"].lower()]]
        net = dev["nets"][term]
        v = pins.get(net) or pins.get(f"/{net}") or {}
        if v.get("power") == 0:
            continue
        nets = {BJT_TERMINALS[k]: n for k, n in dev["nets"].items()}
        if not v:
            on = "an internal net"
        elif "power" in v:
            on = f"a {v['power']:g} V supply pin"
        else:
            on = f"a pin that swings {_fmt_range(v)}"
        out.append(checklib.violation(
            TOOL, "error", where, topcell, "substrate_not_ground",
            [dev["name"]] + sorted(set(nets.values())),
            f"{dev['name']} {dev['model']}: its {BJT_TERMINALS[term]} is the "
            f"p-substrate, but it sits on {net} ({on}), not on a 0 V ground "
            "pin - tie the substrate (a psub tap) to vss", "cvc",
            device=dev["name"], model=dev["model"], nets=nets,
            terminal=BJT_TERMINALS[term]))
    return out


def check(ws: Path, topcell: str, spice_text: str, ext_text: str,
          vdd: float, overrides: dict, work: Path,
          where: str = "layout") -> tuple[list[dict], dict]:
    """Run CVC on one netlist: (violations, facts)."""
    devices, ports, cdl = to_cdl(spice_text, topcell)
    has_ground0 = any("0" in d["nets"].values() for d in devices.values())
    pins = pin_voltages(ports, has_ground0, vdd, overrides)
    log, err = run_cvc(work, topcell, cdl, model_file(devices), power_file(pins))
    counts = parse_counts(log)
    entries = parse_errors(err)
    violations = substrate_findings(topcell, devices, pins, where) + findings(
        ws, topcell, entries, counts, devices, ext_devices(ext_text),
        load_waivers(ws), where)
    facts = {"devices": len({d["name"] for d in devices.values()}), "pins": pins,
             "cvc_counts": {k: v for k, v in counts.items() if v},
             "models": sorted({d["model"] for d in devices.values()})}
    return violations, facts


def schematic_netlist(text: str) -> tuple[str, str]:
    """A netlist/<block>.cir as to_cdl reads it: (top cell, netlist). The
    .subckt line's `name=value` defaults (and any `.param`) are substituted
    into the device lines' `{name}` and dropped. One top .subckt only; a
    `{...}` left unresolved refuses."""
    lines = []
    for raw in text.splitlines():
        if raw.startswith("+") and lines:
            lines[-1] += " " + raw[1:]
        else:
            lines.append(raw)
    params, out, top = {}, [], None
    for line in lines:
        t = line.split()
        if not t or t[0].startswith("*"):
            continue
        head = t[0].lower()
        if head == ".param":
            params.update(_params(t[1:]))
            continue
        if head == ".subckt":
            if top is not None:
                raise CheckError("the schematic has more than one .subckt; "
                                 "the ERC reads one flat block")
            top = t[1]
            params.update(_params([x for x in t[2:] if "=" in x]))
            line = " ".join(x for x in t if "=" not in x)
        out.append(line)
    if top is None:
        raise CheckError("the schematic has no .subckt")
    body = "\n".join(out) + "\n"
    body = re.sub(r"\{\s*(\w+)\s*\}",
                  lambda m: params.get(m.group(1).lower(), m.group(0)), body)
    left = re.search(r"\{[^}]*\}", body)
    if left:
        raise CheckError(f"the schematic's {left.group(0)} is not a .subckt "
                         "default or .param - the ERC cannot size that device")
    return top, body


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="block workspace")
    ap.add_argument("--block", help="override state.json's 'block'")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    ap.add_argument("--schematic", action="store_true",
                    help="check netlist/<block>.cir instead of the layout "
                         "(for a block with no layout yet)")
    args = ap.parse_args(argv)

    ws = Path(args.workspace).resolve()
    spec_path = ws / SPEC_REL
    if not spec_path.is_file():
        raise CheckError(f"no {SPEC_REL}: the ERC needs supply.vdd")
    spec = speclib.load_spec(spec_path)
    vdd = (spec.get("supply") or {}).get("vdd")
    if isinstance(vdd, bool) or not isinstance(vdd, (int, float)) or vdd <= 0:
        raise CheckError(f"{SPEC_REL} has no positive supply.vdd - run spec_lint")
    overrides = speclib.erc_nets(spec)
    if overrides is None:
        raise CheckError(f"{SPEC_REL} 'erc_nets' is malformed - run spec_lint")

    if args.schematic:
        block = layout_gen.block_of(ws, args.block)
        cir = ws / "netlist" / f"{block}.cir"
        if not cir.is_file():
            raise CheckError(f"no netlist/{block}.cir to check")
        rel = str(cir.relative_to(ws))
        topcell, text = schematic_netlist(
            cir.read_text(encoding="utf-8", errors="replace"))
        work = ws / "log" / "erc_schematic"
        violations, facts = check(ws, topcell, text, "", float(vdd),
                                  overrides, work, where=rel)
        payload = checklib.report(SCRIPT, ws / "netlist", violations,
                                  topcell=topcell, netlist=rel,
                                  report="log/erc_schematic/cvc.error.gz",
                                  **facts)
        return payload, args.out

    gds_path, topcell, _abs, _abstract = layout_gen.build(ws, args.block)
    work = ws / "log" / "erc"
    work.mkdir(parents=True, exist_ok=True)
    spice_path, _log = layoutlib.run_magic_extract(
        work, gds_path.resolve(), topcell, parasitics=False)
    ext_path = spice_path.with_suffix(".ext")
    violations, facts = check(
        ws, topcell, spice_path.read_text(encoding="utf-8", errors="replace"),
        ext_path.read_text(encoding="utf-8", errors="replace")
        if ext_path.is_file() else "", float(vdd), overrides, work)
    payload = checklib.report(SCRIPT, ws / "layout", violations,
                              topcell=topcell, gds=str(gds_path.relative_to(ws)),
                              netlist=str(spice_path.relative_to(ws)),
                              report="log/erc/cvc.error.gz", **facts)
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
