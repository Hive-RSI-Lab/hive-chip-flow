"""engine/scripts/check_analog_erc.py: the ade `erc` gate (CVC reliability
ERC on the magic extraction, GitHub issue 122).

Fast tests read canned CVC output (tests/fixtures/analog_erc, from a real
cvc_rv run on corpus mirror with a PMOS whose nwell tap is on vss). Slow
tests run magic and cvc_rv for real: the clean mirror passes, the
nwell-on-vss MOS cap fixture fails without its waiver and passes with it,
the Q class's PNP toys fail by junction, and the bandgap's schematic
passes (--schematic: it has no layout yet).
The mirror's planted faults run in test_corpus_faults.py."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
SCRIPTS = ENGINE / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_analog_erc as erc  # noqa: E402
import faults  # noqa: E402
import gate  # noqa: E402
import layoutlib  # noqa: E402
from checklib import CheckError  # noqa: E402
from test_check_analog_lvs import make_ws  # noqa: E402

FIX = REPO / "tests" / "fixtures" / "analog_erc"
SPICE = (FIX / "current_mirror.spice").read_text(encoding="utf-8")
EXT = (FIX / "current_mirror.ext").read_text(encoding="utf-8")
ERRORS = (FIX / "cvc.error").read_text(encoding="utf-8")
COUNTS = (FIX / "cvc.counts").read_text(encoding="utf-8")

PMOS_CAP_WAIVER = {"device": "X0", "model": "pfet_03v3", "kind": "bulk_tie",
                   "nets": {"d": "vdd", "g": "vdd", "s": "vdd", "b": "vss"},
                   "reason": "test", "owner": "test"}


def _findings(tmp_path, waivers=(), counts=None):
    devices, _ports, _cdl = erc.to_cdl(SPICE, "current_mirror")
    return erc.findings(tmp_path, "current_mirror", erc.parse_errors(ERRORS),
                        erc.parse_counts(COUNTS) if counts is None else counts,
                        devices, erc.ext_devices(EXT), list(waivers))


def test_to_cdl_renames_fets_and_keeps_ports():
    devices, ports, cdl = erc.to_cdl(SPICE, "current_mirror")
    assert ports == ["iref_node", "iout", "vdd", "vss"]
    assert set(devices) == {"M0", "M1", "M2"}
    assert devices["M0"]["name"] == "X0"
    assert devices["M0"]["nets"] == {"d": "vdd", "g": "vdd", "s": "vdd", "b": "vss"}
    assert "M0 vdd vdd vdd vss pfet_03v3 w=4u l=0.5u" in cdl
    assert "X0" not in cdl


def test_to_cdl_refuses_a_device_with_no_rating():
    bad = SPICE.replace("pfet_03v3 ad", "pfet_99v9 ad")
    with pytest.raises(CheckError, match="no voltage rating"):
        erc.to_cdl(bad, "current_mirror")


def test_to_cdl_refuses_the_wrong_top_cell():
    with pytest.raises(CheckError, match="expected one flat"):
        erc.to_cdl(SPICE, "other")


def test_model_file_rates_3v3_devices():
    devices, _p, _c = erc.to_cdl(SPICE, "current_mirror")
    text = erc.model_file(devices)
    assert "MP pfet_03v3 Vth=-0.6 Vgs=3.63" in text
    assert "MN nfet_03v3 Vth=0.6 Vgs=3.63" in text


def test_pin_voltages_defaults_and_overrides():
    pins = erc.pin_voltages(["iref_node", "iout", "vdd", "vss"], True, 3.3,
                            {"iref_node": {"min": 0.0, "max": 0.6}})
    assert pins["vdd"] == {"power": 3.3}
    assert pins["vss"] == {"power": 0.0}
    assert pins["iout"] == {"min": 0.0, "max": 3.3}
    assert pins["iref_node"] == {"min": 0.0, "max": 0.6}
    assert pins["/0"] == {"power": 0.0}
    assert "vdd power 3.3" in erc.power_file(pins)
    assert "iout input min@0 max@3.3" in erc.power_file(pins)


def test_pin_voltages_refuses_an_unknown_erc_net():
    with pytest.raises(CheckError, match="not pins"):
        erc.pin_voltages(["vdd", "vss"], False, 3.3, {"nope": {"power": 1.0}})


def test_parse_counts_reads_the_summary():
    counts = erc.parse_counts(COUNTS)
    assert counts["PMOS Source vs Bulk"] == 1
    assert counts["Voltage Conflicts"] == 0
    assert "Total" not in counts


def test_parse_errors_reads_the_pmos_entry():
    entries = erc.parse_errors(ERRORS)
    assert len(entries) == 1
    e = entries[0]
    assert e["device"] == "M0" and e["model"] == "pfet_03v3"
    assert "pmos source/drain vs bias" in e["section"]
    assert e["terms"]["B"] == {"net": "vss", "min": 0.0, "max": 0.0}
    assert e["terms"]["G"] == {"net": "vdd", "min": 3.3, "max": 3.3}


def test_parse_errors_reads_overvoltage_header_and_mv():
    text = ("! Checking Vgs overvoltage errors\n"
            "Overvoltage Error:Gate vs Source/Drain: Vgs=3.63 \n"
            "/M0 M nfet_03v3 w=8u l=0.5u (r=438)\n"
            "D: iout\n Min: iout@0 r=0\n Sim: iout\n Max: iout@6000 r=0\n"
            "! Checking Vgs overvoltage errors\n")
    e = erc.parse_errors(text)[0]
    assert e["header"].startswith("Overvoltage Error")
    assert e["terms"]["D"] == {"net": "iout", "min": 0.0, "max": 6.0}
    assert erc.section_kind(e["section"])[0] == "overvoltage_vgs"


def test_bulk_tie_finding_names_device_terminal_voltage_and_position(tmp_path):
    out = _findings(tmp_path)
    assert len(out) == 1
    v = out[0]
    assert v["kind"] == "bulk_tie" and v["severity"] == "error"
    assert v["device"] == "X0" and v["model"] == "pfet_03v3"
    assert {"X0", "vdd", "vss"} <= set(v["refs"])
    assert v["terminals"]["bulk"] == {"net": "vss", "min_v": 0.0, "max_v": 0.0}
    assert v["position"] == [12.47, 0.0]
    assert "bulk vss 0 V" in v["msg"]


def test_waiver_turns_the_finding_into_info(tmp_path):
    out = _findings(tmp_path, [PMOS_CAP_WAIVER])
    assert [v["severity"] for v in out] == ["info"]
    assert out[0]["waived"] == {"reason": "test", "owner": "test"}


def test_waiver_on_other_nets_does_not_match(tmp_path):
    w = dict(PMOS_CAP_WAIVER, nets={"d": "vdd", "g": "vdd", "s": "vdd", "b": "vdd"})
    out = _findings(tmp_path, [w])
    kinds = sorted((v["kind"], v["severity"]) for v in out)
    assert kinds == [("bulk_tie", "error"), ("erc_waiver_unused", "error")]


def test_parallel_fingers_are_one_finding(tmp_path):
    devices, _ports, _cdl = erc.to_cdl(SPICE, "current_mirror")
    devices["M9"] = dict(devices["M0"], name="X9")
    entries = erc.parse_errors(ERRORS)
    entries.append(dict(entries[0], device="M9"))
    counts = dict(erc.parse_counts(COUNTS), **{"PMOS Source vs Bulk": 2})
    waiver = dict(PMOS_CAP_WAIVER, device="X9")
    out = erc.findings(tmp_path, "current_mirror", entries, counts, devices,
                       erc.ext_devices(EXT), [waiver])
    assert len(out) == 1
    assert out[0]["device"] == "X0" and out[0]["parallel"] == ["X9"]
    assert "X0 (+1 parallel) pfet_03v3" in out[0]["msg"]
    assert out[0]["severity"] == "info"


def test_count_with_no_entry_is_erc_unparsed(tmp_path):
    counts = dict(erc.parse_counts(COUNTS), **{"HI-Z Inputs": 2})
    out = _findings(tmp_path, counts=counts)
    assert any(v["kind"] == "erc_unparsed" and "HI-Z Inputs" in v["refs"]
               for v in out)


@pytest.mark.parametrize("doc", [
    "waivers: [{device: X0}]",
    "waivers:\n  - " + yaml.safe_dump(dict(PMOS_CAP_WAIVER, reason=""),
                                      default_flow_style=True).strip(),
    "other: 1",
    "waivers: [",
])
def test_bad_waiver_file_refuses(tmp_path, doc):
    (tmp_path / "spec").mkdir()
    (tmp_path / erc.WAIVERS_REL).write_text(doc, encoding="utf-8")
    with pytest.raises(CheckError):
        erc.load_waivers(tmp_path)


def test_run_cvc_refuses_a_model_file_cvc_rejected(tmp_path, monkeypatch):
    def fake(cmd, cwd, timeout):
        (Path(cwd) / "cvc.log").write_text("ERROR: Invalid model file\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(layoutlib, "run_eda", fake)
    with pytest.raises(CheckError, match="could not load"):
        erc.run_cvc(tmp_path, "t", "", "", "")


def test_run_cvc_refuses_a_log_with_no_counts(tmp_path, monkeypatch):
    def fake(cmd, cwd, timeout):
        (Path(cwd) / "cvc.log").write_text("CVC: Start\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(layoutlib, "run_eda", fake)
    with pytest.raises(CheckError, match="no error-count summary"):
        erc.run_cvc(tmp_path, "t", "", "", "")


def test_check_expect_refs_must_all_be_on_one_finding():
    result = {"status": "fail", "failing": [
        {"kind": "bulk_tie", "refs": ["X0", "vdd"]},
        {"kind": "bulk_tie", "refs": ["X1", "vss"]}]}
    expect = {"status": "fail", "kind": "bulk_tie"}
    assert faults.check_expect("f", result, dict(expect, refs=["X0", "vdd"])) == []
    problems = faults.check_expect("f", result, dict(expect, refs=["vdd", "vss"]))
    assert len(problems) == 1 and "naming" in problems[0]


# --- real magic + cvc_rv -----------------------------------------------------

PNP_ON_VBAT = """.subckt t vbat vss
X0 vss vss vbat pnp_10p00x10p00
.ends
"""


def test_to_cdl_splits_a_bjt_into_rated_junctions():
    devices, _ports, cdl = erc.to_cdl(
        PNP_ON_VBAT.replace(".ends", "X1 c b e vss npn_05p00x05p00\n.ends"), "t")
    # PNP: collector = substrate, base = nwell, emitter = P+
    assert "DQ0_eb vbat vss pnp_10p00x10p00_eb" in cdl
    assert "DQ0_ce vss vbat pnp_10p00x10p00_ce" in cdl
    assert "DQ0_cb vss vss pnp_10p00x10p00_cb" in cdl
    assert "RQ0_eb vbat vss pnp_10p00x10p00_on w=1u l=1u" in cdl
    assert "DQ1_sc vss c npn_05p00x05p00_sc" in cdl
    assert "X0" not in cdl
    assert {d["name"] for d in devices.values()} == {"X0", "X1"}
    assert devices["DQ0_eb"]["nets"] == {"c": "vss", "b": "vss", "e": "vbat"}
    assert devices["RQ0_eb"]["path"] is True


def test_to_cdl_refuses_a_bjt_with_the_wrong_terminal_count():
    with pytest.raises(CheckError, match="no voltage rating"):
        erc.to_cdl(".subckt t a b\nX0 a b pnp_10p00x10p00\n.ends\n", "t")


def test_model_file_rates_each_junction_and_forward_checks_the_substrate():
    devices, _ports, _cdl = erc.to_cdl(PNP_ON_VBAT, "t")
    models = erc.model_file(devices).splitlines()
    assert "D pnp_10p00x10p00_eb diode=(1-1) Vds=3.63" in models
    assert "D pnp_10p00x10p00_ce diode=(1-1) Vds=3.63" in models
    assert "D pnp_10p00x10p00_cb Vds=3.63" in models
    assert "R pnp_10p00x10p00_on R=l/w*1000" in models


def test_volts_reads_a_resistively_reached_voltage():
    assert erc._volts("Max: vss@0(5000) r=1000") == 5.0
    assert erc._volts("vss@0") == 0.0
    assert erc._volts("Vds=3300") == 3.3


def test_schematic_netlist_substitutes_subckt_defaults():
    top, body = erc.schematic_netlist(
        "* x\n.param w0=2e-6\n.subckt b a vss rl=11e-6\n"
        "xr1 a vss ppolyf_u_1k r_width={w0}\n+r_length={ rl }\n.ends b\n")
    assert top == "b"
    assert ".subckt b a vss\n" in body
    assert "xr1 a vss ppolyf_u_1k r_width=2e-6 r_length=11e-6" in body


@pytest.mark.parametrize("text, match", [
    (".subckt b a\nxr1 a b ppolyf_u_1k r_length={rl}\n.ends\n", "not a .subckt default"),
    (".subckt a x\n.ends\n.subckt b y\n.ends\n", "more than one"),
    ("xr1 a b ppolyf_u_1k\n", "no .subckt"),
])
def test_schematic_netlist_refuses(text, match):
    with pytest.raises(CheckError, match=match):
        erc.schematic_netlist(text)


@pytest.mark.parametrize("collector, pins, on", [
    ("vss", {"vss": {"power": 0.0}}, None),
    ("vdd", {"vdd": {"power": 3.3}}, "a 3.3 V supply pin"),
    ("n1", {}, "an internal net"),
    ("io", {"io": {"min": 0.0, "max": 3.3}}, "a pin that swings 0..3.3 V"),
])
def test_substrate_findings_need_the_collector_on_ground(collector, pins, on):
    devices, _ports, _cdl = erc.to_cdl(
        f".subckt t e\nX0 {collector} vss e pnp_05p00x05p00\n.ends\n", "t")
    found = erc.substrate_findings("t", devices, pins)
    if on is None:
        assert found == []
        return
    assert [(v["kind"], v["severity"], v["terminal"]) for v in found] == [
        ("substrate_not_ground", "error", "collector")]
    assert on in found[0]["msg"]
    assert found[0]["refs"][0] == "X0"


MOS_CAP = """* a PLL-style MOS cap: a pfet's gate, source and drain on one net and its
* nwell tied to vss, so the nwell to diffusion junction can forward bias
.subckt mos_cap f vss
X0 f f f vss pfet_03v3 w=4u l=0.5u
.ends
"""


@pytest.mark.slow
def test_mirror_passes_the_real_erc(tmp_path):
    ws = make_ws(tmp_path, "mirror")
    gates = gate.load_gates(ENGINE / "reference" / "gates.yaml")
    report, result = faults.run_gate(ws, "erc", gates, "ade")
    assert result["status"] == "pass", report


@pytest.mark.slow
def test_nwell_on_vss_mos_cap_needs_its_waiver(tmp_path):
    ws = tmp_path / "ws"
    (ws / "spec").mkdir(parents=True)
    run = lambda d: erc.check(ws, "mos_cap", MOS_CAP, "", 3.3, {}, tmp_path / d)  # noqa: E731
    violations, _facts = run("bare")
    assert [(v["kind"], v["severity"]) for v in violations] == [("bulk_tie", "error")]
    assert violations[0]["terminals"]["bulk"]["net"] == "vss"

    waiver = dict(PMOS_CAP_WAIVER, nets={"d": "f", "g": "f", "s": "f", "b": "vss"},
                  reason="MOS cap: f never goes below vss in this design",
                  owner="block owner")
    (ws / erc.WAIVERS_REL).write_text(yaml.safe_dump({"waivers": [waiver]}),
                                      encoding="utf-8")
    violations, _facts = run("waived")
    assert [(v["kind"], v["severity"]) for v in violations] == [("bulk_tie", "info")]
    assert violations[0]["waived"]["owner"] == "block owner"


@pytest.mark.slow
def test_pnp_emitter_on_a_5v_net_is_a_junction_overvoltage(tmp_path):
    violations, _facts = erc.check(tmp_path, "t", PNP_ON_VBAT, "", 3.3,
                                   {"vbat": {"min": 0, "max": 5.0}}, tmp_path / "w")
    assert sorted((v["kind"], v["junction"]) for v in violations) == [
        ("overvoltage_junction", "ce"), ("overvoltage_junction", "eb")]
    eb = next(v for v in violations if v["junction"] == "eb")
    assert eb["refs"] == ["X0", "vbat", "vss"]
    assert eb["terminals"]["emitter"] == {"net": "vbat", "min_v": 0.0, "max_v": 5.0}


@pytest.mark.slow
def test_pnp_collector_on_vdd_is_not_a_substrate(tmp_path):
    violations, _facts = erc.check(
        tmp_path, "t", ".subckt t vdd e vss\nX0 vdd vss e pnp_05p00x05p00\n.ends\n",
        "", 3.3, {}, tmp_path / "w")
    assert sorted(v["kind"] for v in violations) == [
        "substrate_not_ground", "substrate_tie"]


@pytest.mark.slow
def test_bandgap_schematic_passes_the_real_erc(tmp_path):
    ws = make_ws(tmp_path, "bandgap")
    payload, _out = erc.run(["--workspace", str(ws), "--schematic"])
    assert payload["status"] == "pass", payload
    assert payload["netlist"] == "netlist/bandgap.cir"
    assert "pnp_10p00x10p00" in payload["models"]
