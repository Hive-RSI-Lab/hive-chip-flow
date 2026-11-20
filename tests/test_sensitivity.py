"""engine/scripts/sensitivity.py (#104): d(measure)/d(param) for each
sizing parameter at a failing corner, and fix_dispatch attaching the top 3
to a sizing work order. The fast tests fake `eda` with a known function of
the deck's `.param` line (y = a^2 * sqrt(b), times 1.3 at the ss corner),
so the ranking and the numbers are checked against exact values with no
ngspice. The slow tests run the real gf180mcuD toolchain: an R2R ladder's
dVout/dR_i against its nodal solve, the corpus mirror's d(iout)/d(iref)
against ngspice's own `.sens`, and a planted short output L on the mirror
ranked first. The mismatch tests (#386) give the fake two devices' z
parameters with known per-sigma terms; the slow ones check the mirror's
summed device variance against check_mc's MC sigma, and that enlarging the
top-ranked device cuts that sigma more than enlarging the second."""
from __future__ import annotations

import json
import re
import shutil
import stat
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import fix_dispatch  # noqa: E402
import sensitivity  # noqa: E402
import sim_run  # noqa: E402
from checklib import CheckError  # noqa: E402

SPEC_YAML = "top: widget\nsupply: {vdd: 3.3}\ndevices: [xm1]\nrequirements: []\n"
BENCH = """\
.lib '{{PDK}}/libs.tech/ngspice/sm141064.spice' {{CORNER}}
.include '{{NETLIST}}'
{{SIZING}}
.end
"""

FAKE = '''\
import re, sys
if sys.argv[1] == "--print-toolchain-root":
    print(ROOT)
    sys.exit(0)
deck = open(sys.argv[3]).read()
p = {k: float(v) for k, v in re.findall(r"(\\w+)=([-0-9.eE+]+)",
     re.search(r"^\\.param (.*)$", deck, re.M).group(1))}
corner = re.search(r"sm141064.spice' (\\w+)", deck).group(1)
if p.get("crash", 7.0) != 7.0 or p.get("edge", 1.0) < 1.0:
    sys.exit(0)
y = (p["a"] ** 2 * p["b"] ** 0.5 * p.get("edge", 1.0) ** 3
     * (1.3 if corner == "ss" else 1.0))
# #386: two devices' mismatch z parameters, known per-sigma terms
y += (0.03 * p.get("mm_zv_0", 0) + 0.04 * p.get("mm_zk_0", 0)
      + 0.01 * p.get("mm_zv_1", 0))
print()
print("  Measurements for Transient Analysis")
print(f"y                     =  {y!r}")
'''


# the shape of the PDK's `.lib fets_mm` MOS subckts (sm141064.spice)
PDK_LIB = """\
.subckt nfet_03v3 d g s b w=1e-5 l=2.8e-7 m=1 par=1
.param var_vth='0.7071*0.007*1e-06/sqrt(par*w*l)'
.param mis_vth='agauss(0,var_vth,1)'
.param var_k='0.7071*0.007*1e-06/sqrt(par*w*l)'
.param mis_k='agauss(0,var_k,1)'
m0 d g s b nfet_03v3 delvto='mis_vth*sw_stat_mismatch' l=l m=m mulu0='1-mis_k*sw_stat_mismatch' w=w
.ends nfet_03v3
.subckt pfet_03v3 d g s b w=1e-5 l=2.8e-7 m=1 par=1
m0 d g s b pfet_03v3 delvto=0 l=l m=m w=w
.ends pfet_03v3
"""


def make_fake_eda(tmp_path: Path) -> Path:
    root = tmp_path / "fake_toolchain"
    lib = root / "foss" / "pdks" / "gf180mcuD" / "libs.tech" / "ngspice"
    lib.mkdir(parents=True)
    (lib / "sm141064.spice").write_text(PDK_LIB, encoding="utf-8")
    script = tmp_path / "fake_eda"
    script.write_text(f"#!{sys.executable}\nROOT = {str(root)!r}\n" + FAKE,
                      encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def make_ws(tmp_path: Path, sizing: dict) -> Path:
    import yaml
    ws = tmp_path / "ws"
    for sub in ("spec", "netlist", "tb", "sizing"):
        (ws / sub).mkdir(parents=True)
    (ws / "spec" / "spec.yaml").write_text(SPEC_YAML, encoding="utf-8")
    (ws / "netlist" / "widget.cir").write_text("* netlist\n", encoding="utf-8")
    (ws / "tb" / "widget_tb.cir").write_text(BENCH, encoding="utf-8")
    (ws / "tb" / "widget_tb.bounds.json").write_text(
        json.dumps([{"measure": "y", "min": 0.9, "max": 1.1}]),
        encoding="utf-8")
    (ws / "sizing" / "sizing.yaml").write_text(yaml.safe_dump(sizing),
                                               encoding="utf-8")
    return ws


SIZING = {"a": {"value": 1.2, "min": 0.5, "max": 2.0},
          "b": {"value": 1.0, "min": 0.5, "max": 2.0},
          "c": {"value": 3.0},
          "mode": {"value": "fast"}}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    eda = make_fake_eda(tmp_path)
    monkeypatch.setattr(sim_run, "EDA_BIN", eda)
    return eda


def test_ranks_by_normalised_sensitivity(tmp_path, fake):
    ws = make_ws(tmp_path, SIZING)
    rep = sensitivity.analyse(ws, "y", corner_name="tt")
    assert rep["status"] == "pass" and rep["corner"] == "tt"
    assert rep["value"] == pytest.approx(1.44) and not rep["passes"]
    by = {e["param"]: e for e in rep["params"]}
    # y = a^2: the central difference of a quadratic is exact
    assert by["a"]["derivative"] == pytest.approx(2 * 1.2)
    assert by["a"]["normalised"] == pytest.approx(2.0)
    assert by["b"]["normalised"] == pytest.approx(0.5, rel=1e-3)
    assert by["c"]["normalised"] == 0.0
    assert [e["param"] for e in rep["params"]] == ["a", "b", "c"]
    assert rep["top"] == ["a", "b", "c"]
    # 1.44 -> 1.1 along d/da: a step of (1.1-1.44)/(2.4*1.2), down, and
    # a's own min 0.5 leaves (0.5-1.2)/1.2 of room
    assert by["a"]["to_bound"] == pytest.approx(-0.34 / 2.88)
    assert by["a"]["room"] == pytest.approx(-0.7 / 1.2)
    assert by["c"]["to_bound"] is None
    assert rep["skipped"] == [{"param": "mode",
                               "reason": "non-numeric value"}]


def test_missing_measure_ranks_last_never_zero(tmp_path, fake):
    ws = make_ws(tmp_path, {**SIZING, "crash": {"value": 7.0}})
    rep = sensitivity.analyse(ws, "y", corner_name="tt", top=4)
    last = rep["params"][-1]
    assert last["param"] == "crash"
    assert last["derivative"] is None and last["normalised"] is None
    assert "y" in last["error"]
    assert "crash" not in rep["top"]


def test_one_failed_step_is_a_flagged_one_sided_difference(tmp_path, fake):
    ws = make_ws(tmp_path, {**SIZING, "edge": {"value": 1.0}})
    rep = sensitivity.analyse(ws, "y", corner_name="tt")
    e = {p["param"]: p for p in rep["params"]}["edge"]
    assert e["one_sided"] == "up" and "y" in e["error"]
    assert e["normalised"] == pytest.approx((1.05 ** 3 - 1) / 0.05)
    assert rep["top"][0] == "edge"


def test_without_corner_picks_least_margin(tmp_path, fake):
    ws = make_ws(tmp_path, {**SIZING, "a": {"value": 1.0}})
    rep = sensitivity.analyse(ws, "y")
    assert rep["corner"] == "ss"
    assert rep["value"] == pytest.approx(1.3)


def test_unknown_measure_and_corner_refused(tmp_path, fake):
    ws = make_ws(tmp_path, SIZING)
    with pytest.raises(CheckError, match="declares"):
        sensitivity.analyse(ws, "nope", corner_name="tt")
    with pytest.raises(CheckError, match="sweep"):
        sensitivity.analyse(ws, "y", corner_name="zz_nowhere")


def test_bound_fail_pairs_reads_pad_condition():
    plain = {"kind": "sim_bound_fail", "refs": ["y", "ss"],
             "msg": "y @ ss = 1.3 outside bound [0.9, 1.1]"}
    pad = {"kind": "sim_bound_fail", "refs": ["pad", "ss", "y"],
           "condition": "pad", "msg": "[pad] y @ ss = 1.3 outside bound"}
    other = {"kind": "sim_engine_error", "refs": ["x"], "msg": "x"}
    assert fix_dispatch.bound_fail_pairs([plain, plain, pad, other]) == [
        ("y", "ss", None), ("y", "ss", "pad")]


def _gate_result(tmp_path: Path) -> Path:
    v = {"check": "sim_tt", "severity": "error", "file": "widget_tb.cir",
         "module": None, "line": None, "refs": ["y", "tt"],
         "msg": "y @ tt = 1.44 outside bound [0.9, 1.1]",
         "source": "ngspice", "kind": "sim_bound_fail"}
    inp = tmp_path / "r.json"
    inp.write_text(json.dumps({"gate": "sim_tt", "violations": [v]}),
                   encoding="utf-8")
    return inp


def test_fix_dispatch_attaches_top_three(tmp_path, fake):
    ws = make_ws(tmp_path, SIZING)
    payload, _ = fix_dispatch.run(["--input", str(_gate_result(tmp_path)),
                                   "--workspace", str(ws)])
    (order,) = payload["orders"]
    assert order["fixer"] == "sizing"
    wo = json.loads(Path(order["work_order"]).read_text(encoding="utf-8"))
    (sens,) = wo["sensitivity"]
    assert (sens["measure"], sens["corner"]) == ("y", "tt")
    assert [e["param"] for e in sens["top"]] == ["a", "b", "c"]
    assert wo["guidance"][0] == fix_dispatch.SENSITIVITY_GUIDANCE

    payload, _ = fix_dispatch.run(["--input", str(_gate_result(tmp_path)),
                                   "--workspace", str(ws),
                                   "--no-sensitivity"])
    wo = json.loads(Path(payload["orders"][0]["work_order"]).read_text())
    assert "sensitivity" not in wo


def test_fix_dispatch_records_a_failed_run(tmp_path, fake):
    ws = make_ws(tmp_path, SIZING)
    (ws / "tb" / "widget_tb.bounds.json").write_text(
        json.dumps([{"measure": "z", "min": 0, "max": 1}]), encoding="utf-8")
    payload, _ = fix_dispatch.run(["--input", str(_gate_result(tmp_path)),
                                   "--workspace", str(ws)])
    wo = json.loads(Path(payload["orders"][0]["work_order"]).read_text())
    (sens,) = wo["sensitivity"]
    assert "declares" in sens["error"]


# ------------------------------------------------------------ real ngspice

R2R_NETLIST = """\
* 2-bit R-2R ladder of ideal resistors: b1 (MSB) -> n1 -> out, b0 -> n0
.subckt r2r b0 b1 out vss
r_2lsb b0 n0 {r_2lsb}
r_term n0 vss 20k
r_lsb n0 out {r_lsb}
r_2msb b1 out {r_2msb}
r_msb out vss {r_msb}
.ends
"""
R2R_BENCH = """\
* r2r sensitivity bench - ideal resistors, no PDK models
.temp {{TEMP_C}}
{{SIZING}}
.include '{{NETLIST}}'
vb0 b0 0 3.3
vb1 b1 0 0
xdut b0 b1 out 0 r2r
.tran 1n 5n
.measure tran vout_lsb FIND v(out) AT=4n
.end
"""
R2R = {"r_2lsb": 20e3, "r_lsb": 10e3, "r_2msb": 20e3, "r_msb": 20e3}


def r2r_vout(r: dict) -> float:
    """Nodal solve of the bench above: b0 = 3.3, b1 = 0."""
    import numpy as np
    g2l, gt, gl = 1 / r["r_2lsb"], 1 / 20e3, 1 / r["r_lsb"]
    g2m, gm = 1 / r["r_2msb"], 1 / r["r_msb"]
    a = np.array([[g2l + gt + gl, -gl], [-gl, gl + g2m + gm]])
    return float(np.linalg.solve(a, [3.3 * g2l, 0.0])[1])


def real_ws(tmp_path: Path, netlist: str, bench: str, bounds: list,
            sizing: dict, spec: str) -> Path:
    import yaml
    ws = tmp_path / "ws"
    for sub in ("spec", "netlist", "tb", "sizing"):
        (ws / sub).mkdir(parents=True)
    (ws / "spec" / "spec.yaml").write_text(spec, encoding="utf-8")
    (ws / "netlist" / "dut.cir").write_text(netlist, encoding="utf-8")
    (ws / "tb" / "dut_tb.cir").write_text(bench, encoding="utf-8")
    (ws / "tb" / "dut_tb.bounds.json").write_text(json.dumps(bounds),
                                                  encoding="utf-8")
    (ws / "sizing" / "sizing.yaml").write_text(yaml.safe_dump(
        {k: {"value": v} for k, v in sizing.items()}), encoding="utf-8")
    return ws


@pytest.mark.slow
def test_real_r2r_matches_analytic_derivative(tmp_path):
    ws = real_ws(tmp_path, R2R_NETLIST, R2R_BENCH,
                 [{"measure": "vout_lsb", "min": 0.0, "max": 0.5}], R2R,
                 "top: r2r\nsupply: {vdd: 3.3}\ndevices: []\n"
                 "requirements: []\n")
    rep = sensitivity.analyse(ws, "vout_lsb", corner_name="tt", top=4)
    assert rep["value"] == pytest.approx(r2r_vout(R2R), rel=1e-4)
    for e in rep["params"]:
        h = 1e-4 * R2R[e["param"]]
        exact = (r2r_vout({**R2R, e["param"]: R2R[e["param"]] + h})
                 - r2r_vout({**R2R, e["param"]: R2R[e["param"]] - h})) / (2 * h)
        assert e["derivative"] == pytest.approx(exact, rel=0.02), e


MIRROR = REPO / "corpus" / "ade" / "mirror"


def mirror_ws(tmp_path: Path, sizing: dict) -> Path:
    """The corpus mirror with its device sizes and reference current
    lifted into sizing.yaml (the corpus block hard-codes them)."""
    net = (MIRROR / "netlist" / "mirror.cir").read_text(encoding="utf-8")
    net, n_ref = re.subn(r"(xmref\b[^\n]*?)w=4e-6 l=5e-7",
                         r"\1w={w_ref} l={l_ref}", net)
    net, n_out = re.subn(r"(xmout\b[^\n]*?)w=8e-6 l=5e-7",
                         r"\1w={w_out} l={l_out}", net)
    assert n_ref == n_out == 1, "corpus mirror netlist changed shape"
    bench = (MIRROR / "tb" / "mirror_tb.cir").read_text(encoding="utf-8")
    bench, n = re.subn(r"(iref_src\s+\S+\s+\S+\s+dc\s+)10e-6", r"\g<1>{i_ref}",
                       bench)
    assert n == 1, "corpus mirror bench changed shape"
    bounds = json.loads((MIRROR / "tb" / "mirror_tb.bounds.json").read_text())
    return real_ws(tmp_path, net, bench, bounds, sizing,
                   (MIRROR / "spec.yaml").read_text(encoding="utf-8"))


MIRROR_SIZING = {"w_ref": 4e-6, "l_ref": 5e-7, "w_out": 8e-6, "l_out": 5e-7,
                 "i_ref": 10e-6}


@pytest.mark.slow
def test_real_mirror_matches_ngspice_sens(tmp_path):
    ws = mirror_ws(tmp_path, MIRROR_SIZING)
    rep = sensitivity.analyse(ws, "iout_raw", corner_name="tt", top=5)
    ours = {e["param"]: e for e in rep["params"]}["i_ref"]["derivative"]
    # ngspice's own small-signal .sens of the same deck's DC point
    deck = Path(rep["decks"]) / "baseline" / "dut_tb__tt.cir"
    text = deck.read_text(encoding="utf-8")
    text = re.sub(r"^\.(tran|measure)\b.*$", "", text, flags=re.M)
    text = re.sub(r"^\.control.*?^\.endc\s*$", "", text,
                  flags=re.M | re.S)
    text = re.sub(r"^\.end\s*$", ".control\nsens i(vload)\nprint all\n"
                  ".endc\n.end", text, flags=re.M)
    sens_deck = tmp_path / "sens.cir"
    sens_deck.write_text(text, encoding="utf-8")
    import simlib
    out, err, _ = simlib.run_ngspice(sim_run.EDA_BIN, sens_deck,
                                     tmp_path, 120)
    m = re.search(r"^\s*iref_src\s*=\s*([-0-9.eE+]+)", out, re.M)
    assert m, out[-2000:] + err[-2000:]
    assert ours == pytest.approx(float(m.group(1)), rel=0.02)


@pytest.mark.slow
def test_real_mirror_planted_short_output_l_ranks_first(tmp_path):
    ws = mirror_ws(tmp_path, {**MIRROR_SIZING, "l_out": 2.8e-7})
    rep = sensitivity.analyse(ws, "iout_raw", corner_name="tt", top=3)
    assert not rep["passes"] and rep["value"] > rep["bound"]["max"]
    assert rep["params"][0]["param"] == "l_out", rep["params"]
    assert rep["params"][0]["to_bound"] > 0   # lengthen it
    # 0.28u is the 3.3 V device's minimum L: the step down leaves the
    # models' range, so its derivative is the step up's, flagged
    assert rep["params"][0]["one_sided"] == "up"


# ---------------------------------------------------- mismatch (#386), fast

MM_NETLIST = """\
.subckt widget a b vss
xm1 a a vss vss nfet_03v3 w=4e-6 l=5e-7
xm2 b a vss vss nfet_03v3 w=8e-6 l=5e-7 m=2
xr1 a vss vss ppolyf_u r_length=1e-5 r_width=1e-6
xp1 b b vss vss pfet_03v3 w=1e-6 l=1e-6
xu1 a b vss unit
xu2 a b vss unit
.ends widget
.subckt unit d g s
xmu d g s s nfet_03v3 w=1e-6 l=1e-6
.ends unit
"""
MM_BENCH = BENCH.replace("{{SIZING}}", "xdut a b 0 widget\n{{SIZING}}")


def test_mismatch_netlist_parameterises_each_draw():
    netlist = MM_NETLIST.replace("xp1 b b vss vss pfet_03v3 w=1e-6 l=1e-6\n",
                                 "")
    new, devs, unranked = sensitivity.mismatch_netlist(netlist, MM_BENCH,
                                                       PDK_LIB)
    assert [d["device"] for d in devs] == ["widget/xm1", "widget/xm2",
                                           "widget/xr1"]
    assert "xm1 a a vss vss nfet_03v3__mm w=4e-6 l=5e-7 " \
        "mm_zv={mm_zv_0} mm_zk={mm_zk_0}" in new
    assert "m=2 par=2 mm_zv={mm_zv_1}" in new          # par before z
    assert "mis_r='mm_zr_2*(" in new
    sub = new.split("* --- sensitivity.py")[1]
    assert sub.count(".subckt nfet_03v3__mm d g s b mm_zv=0 mm_zk=0 ") == 1
    assert "delvto='mm_zv*var_vth'" in sub and "mulu0='1-mm_zk*var_k'" in sub
    assert "sw_stat_mismatch" not in sub and ".ends nfet_03v3__mm" in sub
    why = {u["device"]: u["reason"] for u in unranked}
    # unit is instantiated twice: retargeted with no z, so no mismatch
    assert set(why) == {"unit/xmu"} and "2 times" in why["unit/xmu"]
    assert "xmu d g s s nfet_03v3__mm w=1e-6 l=1e-6\n" in new
    # with a device the rewrite cannot parameterise, sw_stat_mismatch=1
    # would draw for it too, so the resistor is not ranked either
    blocked = netlist.replace(".ends widget",
                              "xq1 vss vss a pnp_10p00x10p00\n.ends widget")
    _, devs, unranked = sensitivity.mismatch_netlist(blocked, MM_BENCH,
                                                     PDK_LIB)
    assert [d["device"] for d in devs] == ["widget/xm1", "widget/xm2"]
    why = {u["device"]: u["reason"] for u in unranked}
    assert "no parameterised" in why["widget/xq1"]
    assert "widget/xq1" in why["widget/xr1"]
    # so does a MOS whose library subckt lacks the two draws
    _, devs, unranked = sensitivity.mismatch_netlist(MM_NETLIST, MM_BENCH,
                                                     PDK_LIB)
    why = {u["device"]: u["reason"] for u in unranked}
    assert "no pfet_03v3 subckt" in why["widget/xp1"]
    assert "widget/xr1" in why


def test_fix_dispatch_attaches_mismatch_ranking(tmp_path, fake):
    ws = make_ws(tmp_path, {"a": {"value": 1.0}, "b": {"value": 1.0}})
    (ws / "netlist" / "widget.cir").write_text(
        ".subckt widget a vss\nxm1 a a vss vss nfet_03v3 w=4e-6 l=5e-7\n"
        "xm2 a a vss vss nfet_03v3 w=8e-6 l=5e-7\n.ends\n", encoding="utf-8")
    (ws / "tb" / "widget_tb.cir").write_text(MM_BENCH, encoding="utf-8")
    v = {"check": "mc", "severity": "error", "file": None, "module": None,
         "line": None, "refs": ["widget_tb.cir@tt"], "source": "sim_run",
         "msg": "Monte Carlo yield at the tt corner is 80.00%",
         "kind": "yield_below_spec"}
    inp = tmp_path / "mc.json"
    inp.write_text(json.dumps({"gate": "mc", "violations": [v]}),
                   encoding="utf-8")
    payload, _ = fix_dispatch.run(["--input", str(inp),
                                   "--workspace", str(ws)])
    (order,) = payload["orders"]
    wo = json.loads(Path(order["work_order"]).read_text(encoding="utf-8"))
    (mm,) = wo["mismatch"]
    assert (mm["measure"], mm["corner"]) == ("y", "tt")
    # y moves 0.03 and 0.04 per sigma of xm1's vth and k, 0.01 of xm2's
    assert mm["sigma"] == pytest.approx((0.03**2 + 0.04**2 + 0.01**2) ** 0.5)
    top = mm["top"]
    assert [d["device"] for d in top] == ["widget/xm1", "widget/xm2"]
    assert top[0]["per_sigma"] == pytest.approx({"vth": 0.03, "k": 0.04})
    assert top[0]["share"] == pytest.approx(0.0025 / 0.0026)
    assert top[0]["sigma_if_area_doubled"] == pytest.approx(
        (0.0026 - 0.0025 / 2) ** 0.5)
    assert wo["guidance"][0] == fix_dispatch.MISMATCH_GUIDANCE
    assert "sensitivity" not in wo       # no sim_bound_fail in the cluster


# ---------------------------------------------------- mismatch (#386), slow

def _mc_tt_sigma(ws: Path) -> float:
    import check_mc
    d, _ = check_mc.run(["--workspace", str(ws), "--jobs", "4"])
    (g,) = [g for g in d["groups"] if g.get("role") == "tt"]
    return g["stats"]["iout_raw"]["sigma"]


@pytest.mark.slow
def test_real_mirror_mismatch_sums_to_mc_sigma(tmp_path):
    ws = mirror_ws(tmp_path, MIRROR_SIZING)
    rep = sensitivity.analyse_mismatch(ws, "iout_raw", corner_name="tt")
    mc = _mc_tt_sigma(ws)
    assert rep["sigma"] == pytest.approx(mc, rel=0.15), (rep["sigma"], mc)
    assert rep["devices"][0]["device"].endswith("xmref"), rep["devices"]
    assert sum(d["share"] for d in rep["devices"]) == pytest.approx(1.0)


@pytest.mark.slow
def test_real_mirror_enlarging_top_device_cuts_mc_sigma_most(tmp_path):
    ws = mirror_ws(tmp_path, MIRROR_SIZING)
    rep = sensitivity.analyse_mismatch(ws, "iout_raw", corner_name="tt")
    first, second = (d["device"].rsplit("/", 1)[-1][2:]
                     for d in rep["devices"][:2])     # "ref", "out"
    sig = {}
    for dev in (first, second):
        big = {**MIRROR_SIZING, f"w_{dev}": 2 * MIRROR_SIZING[f"w_{dev}"],
               f"l_{dev}": 2 * MIRROR_SIZING[f"l_{dev}"]}
        sig[dev] = _mc_tt_sigma(mirror_ws(tmp_path / dev, big))
    assert sig[first] < sig[second], sig
