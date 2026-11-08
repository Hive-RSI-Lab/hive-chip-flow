"""The stability, PSRR and noise bench templates (issue #100) and the
cross-checks of the Tian loop-gain bench (skills/ade/templates/stb_tb.cir).

The templates' measure names are fixed (fast). The cross-checks are slow
and run on corpus/ade/ota at tt:
  - Tian (ngspice, the loop never broken) against the classic LC
    break-the-loop bench (1 GH in the feedback wire, 1 GF injecting): the
    phase margin within 2 degrees;
  - the LC bench on Xyce against Tian on ngspice: the unity-gain frequency
    within 2%.
The planted comp-cap removal failing the stability bench is the ota rung's
own fault manifest (tests/test_corpus_faults.py)."""
from __future__ import annotations

import csv
import json
import math
import re
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "lib"))

import layoutlib  # noqa: E402
import simlib  # noqa: E402

EDA = REPO / "bin" / "eda"
TEMPLATES = REPO / "skills" / "ade" / "templates"
OTA = REPO / "corpus" / "ade" / "ota"


@pytest.mark.parametrize("name, measures", [
    ("stb_tb.cir", ["pm_deg", "gm_db", "ugf_hz"]),
    ("psrr_tb.cir", ["psrr_dc_db", "psrr_at_f_db"]),
    ("noise_tb.cir", ["onoise_rms_v"])])
def test_template_prints_its_fixed_measure_names(name, measures):
    text = (TEMPLATES / name).read_text(encoding="utf-8")
    printed = set(re.findall(r'^echo "(\w+) = ', text, re.M))
    assert set(measures) <= printed, (name, printed)
    assert "<block>" in text  # the DUT section is the bench writer's


def test_ota_benches_print_the_template_names():
    for bench, template in (("ota_stb_tb.cir", "stb_tb.cir"),
                            ("ota_psrr_tb.cir", "psrr_tb.cir"),
                            ("ota_noise_tb.cir", "noise_tb.cir")):
        names = set(re.findall(r'^echo "(\w+) = ',
                               (TEMPLATES / template).read_text(), re.M))
        got = set(re.findall(r'^echo "(\w+) = ',
                             (OTA / "tb" / bench).read_text(), re.M))
        assert names <= got, (bench, names - got)


# ---- the cross-checks, corpus/ade/ota at tt ----------------------------------
DUT = """\
.include '{{NETLIST}}'
{{SIZING}}
.param vddp=3.3 vcm={vddp/2} ib=10u cl=2p
vdd_src vdd 0 {vddp}
vss_src vss 0 0
vin inp 0 dc {vcm}
ibias_src 0 ibias dc {ib}
xdut inp fb out vdd vss ibias two_stage_miller_ota
cload out 0 {cl}
* the loop broken for AC only: the feedback closes at DC through lbrk,
* cbrk injects the AC test signal at fb
lbrk out fb 1g
cbrk fb vac 1g
vac vac 0 dc 0 ac 1
"""

NGSPICE_LC = """\
* LC break-the-loop, ngspice
.include '{{PDK}}/libs.tech/ngspice/design.spice'
.lib '{{PDK}}/libs.tech/ngspice/sm141064.spice' typical
.lib '{{PDK}}/libs.tech/ngspice/sm141064.spice' mimcap_typical
.temp 27
""" + DUT + """\
.control
set noaskquit
ac dec 40 1 100g
let tl = -v(out)/v(fb)
let tdb = db(tl)
let tph = cph(tl)*180/pi
meas ac ugf_v when tdb=0 fall=1
meas ac ph_ugf find tph at=ugf_v
let pm_v = 180 + ph_ugf
echo "lc_pm_deg = $&pm_v"
echo "lc_ugf_hz = $&ugf_v"
.endc
.end
"""

XYCE_LC = """\
* LC break-the-loop, Xyce
.include '{{PDK}}/libs.tech/xyce/design.xyce'
.lib '{{PDK}}/libs.tech/xyce/sm141064.xyce' typical
.lib '{{PDK}}/libs.tech/xyce/sm141064.xyce' mimcap_typical
.temp 27
""" + DUT + """\
.ac dec 40 1 100g
.print ac format=csv file=lc.csv vr(out) vi(out) vr(fb) vi(fb)
.end
"""


def _subs():
    sizing = yaml.safe_load((OTA / "sizing" / "sizing.yaml").read_text())
    return {"PDK": str(layoutlib.pdk_root()),
            "NETLIST": str(OTA / "netlist" / "ota.cir"),
            "SIZING": simlib.sizing_param_line(sizing)}


def _run(args, cwd):
    proc = subprocess.run([str(EDA), *args], cwd=cwd, capture_output=True,
                          text=True, timeout=300)
    assert proc.returncode == 0, (proc.stdout + proc.stderr)[-2000:]
    return proc.stdout


@pytest.fixture(scope="module")
def tian(tmp_path_factory):
    """ugf_hz/pm_deg from the rung's own Tian bench, through sim_run at tt."""
    ws = tmp_path_factory.mktemp("ota")
    (ws / "spec").mkdir()
    (ws / "spec" / "spec.yaml").write_bytes((OTA / "spec.yaml").read_bytes())
    for sub in ("netlist", "tb", "sizing"):
        (ws / sub).mkdir()
        for f in (OTA / sub).iterdir():
            (ws / sub / f.name).write_bytes(f.read_bytes())
    out = _run(["python", str(REPO / "engine" / "scripts" / "sim_run.py"),
                "--workspace", str(ws), "--corners", "tt"], ws)
    res = [r for r in json.loads(out)["results"]
           if r["bench"] == "ota_stb_tb.cir"]
    assert len(res) == 1
    return res[0]["measures"]


@pytest.mark.slow
def test_tian_phase_margin_matches_the_lc_bench(tian, tmp_path):
    deck = tmp_path / "lc.cir"
    deck.write_text(simlib.materialize(NGSPICE_LC, _subs()))
    out = _run(["ngspice", "-b", str(deck)], tmp_path)
    lc = dict(re.findall(r"^(lc_\w+) = (\S+)", out, re.M))
    assert abs(float(lc["lc_pm_deg"]) - tian["pm_deg"]) <= 2.0, (lc, tian)


def _ugf_from_csv(path: Path) -> float:
    """The first fall of |T| = |v(out)/v(fb)| through 1, log-interpolated."""
    prev = None
    with path.open() as f:
        for row in list(csv.reader(f))[1:]:
            fr, a, b, c, d = map(float, row)
            m = abs(complex(a, b) / complex(c, d))
            if prev and prev[1] >= 1 > m:
                f0, m0 = prev
                t = math.log(m0) / (math.log(m0) - math.log(m))
                return math.exp(math.log(f0) + t * (math.log(fr) - math.log(f0)))
            prev = (fr, m)
    raise AssertionError(f"|T| never falls through 1 in {path}")


@pytest.mark.slow
def test_xyce_lc_unity_gain_frequency_matches_tian(tian, tmp_path):
    deck = tmp_path / "lc.cir"
    deck.write_text(simlib.materialize(XYCE_LC, _subs()))
    _run(["Xyce", str(deck)], tmp_path)
    ugf = _ugf_from_csv(tmp_path / "lc.csv")
    assert abs(ugf - tian["ugf_hz"]) / tian["ugf_hz"] <= 0.02, (ugf, tian)
