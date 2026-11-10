"""engine/scripts/check_split.py: the split gate (docs/design.md 1.5's msde
split row, "### M10."). Pure YAML parsing/comparison - no toolchain, no
eda image, nothing here is `slow`."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
SCRIPTS = ENGINE / "scripts"
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))

import check_split  # noqa: E402
from checklib import CheckError  # noqa: E402

INTERFACE = """\
version: 2
signals:
  - name: osc_out
    direction: a2d
    level: cmos_3v3
    domain: clk_free
    width: 1
    load: {cap_ff: 2000}
  - name: osc_en
    direction: d2a
    level: cmos_3v3
    domain: clk_sys
    width: 1
    load: {cap_ff: 4000}
"""

DIGITAL_SPEC = """\
top: sensor_counted_digital
interface:
  - name: osc_out
    direction: a2d
    level: cmos_3v3
    domain: clk_free
    width: 1
    load: {cap_ff: 2000}
  - name: osc_en
    direction: d2a
    level: cmos_3v3
    domain: clk_sys
    width: 1
    load: {cap_ff: 4000}
"""

ANALOG_SPEC = DIGITAL_SPEC.replace("sensor_counted_digital", "sensor_counted_analog")


def make_ws(tmp_path: Path, digital=DIGITAL_SPEC, analog=ANALOG_SPEC,
           interface=INTERFACE) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "interface.yaml").write_text(interface, encoding="utf-8")
    (ws / "digital_spec.yaml").write_text(digital, encoding="utf-8")
    (ws / "analog_spec.yaml").write_text(analog, encoding="utf-8")
    return ws


def test_clean_passes(tmp_path):
    ws = make_ws(tmp_path)
    payload, _out = check_split.run(["--workspace", str(ws)])
    assert payload["status"] == "pass"
    assert payload["violations"] == []
    assert payload["signals"] == ["osc_en", "osc_out"]


def test_no_interface_yaml_is_an_error(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    try:
        check_split.run(["--workspace", str(ws)])
        assert False, "expected CheckError"
    except CheckError:
        pass


def test_width_mismatch_caught(tmp_path):
    # gates.yaml's own named fault for this gate.
    analog = ANALOG_SPEC.replace(
        "    domain: clk_sys\n    width: 1\n",
        "    domain: clk_sys\n    width: 2\n", 1)
    ws = make_ws(tmp_path, analog=analog)
    payload, _out = check_split.run(["--workspace", str(ws)])
    assert payload["status"] == "violations"
    kinds = {v["kind"] for v in payload["violations"]}
    assert "width_mismatch" in kinds


def test_load_mismatch_caught(tmp_path):
    # `load` used to be read into canon/side dicts but never compared - a
    # differing load slipped through silently.
    analog = ANALOG_SPEC.replace(
        "    width: 1\n    load: {cap_ff: 4000}\n",
        "    width: 1\n    load: {cap_ff: 8000}\n", 1)
    ws = make_ws(tmp_path, analog=analog)
    payload, _out = check_split.run(["--workspace", str(ws)])
    assert payload["status"] == "violations"
    kinds = {v["kind"] for v in payload["violations"]}
    assert "load_mismatch" in kinds


def test_direction_mismatch_caught(tmp_path):
    digital = DIGITAL_SPEC.replace("direction: d2a", "direction: a2d")
    ws = make_ws(tmp_path, digital=digital)
    payload, _out = check_split.run(["--workspace", str(ws)])
    assert payload["status"] == "violations"
    kinds = {v["kind"] for v in payload["violations"]}
    assert "direction_mismatch" in kinds


def test_signal_missing_from_one_spec_caught(tmp_path):
    digital = """\
top: sensor_counted_digital
interface:
  - name: osc_out
    direction: a2d
    level: cmos_3v3
    domain: clk_free
    width: 1
"""
    ws = make_ws(tmp_path, digital=digital)
    payload, _out = check_split.run(["--workspace", str(ws)])
    assert payload["status"] == "violations"
    kinds = {v["kind"] for v in payload["violations"]}
    assert "signal_missing_from_spec" in kinds


def test_signal_not_declared_in_interface_caught(tmp_path):
    digital = DIGITAL_SPEC + """\
  - name: extra_sig
    direction: d2a
    level: cmos_3v3
    domain: clk_sys
    width: 4
"""
    ws = make_ws(tmp_path, digital=digital)
    payload, _out = check_split.run(["--workspace", str(ws)])
    assert payload["status"] == "violations"
    kinds = {v["kind"] for v in payload["violations"]}
    assert "signal_not_declared" in kinds


# ---------------------------------------------------------------- refusals
# Before the fix, an interface.yaml entry missing every field but `name`
# passed clean: canon and both sides read the same missing field as None,
# and None == None on direction, level, domain and width (load was not even
# compared). Each of these drives interface.yaml down to a bare `name` on
# one required field at a time and expects a refusal, not a pass.

def _interface_missing(field: str) -> str:
    lines = [ln for ln in INTERFACE.splitlines(keepends=True)
             if not ln.strip().startswith(f"{field}:")]
    return "".join(lines)


def _assert_missing_field_refused(tmp_path, field: str):
    ws = make_ws(tmp_path, interface=_interface_missing(field))
    try:
        check_split.run(["--workspace", str(ws)])
        assert False, f"expected CheckError for a signal missing {field!r}"
    except CheckError:
        pass


def test_missing_direction_is_refused(tmp_path):
    _assert_missing_field_refused(tmp_path, "direction")


def test_missing_level_is_refused(tmp_path):
    _assert_missing_field_refused(tmp_path, "level")


def test_missing_domain_is_refused(tmp_path):
    _assert_missing_field_refused(tmp_path, "domain")


def test_missing_width_is_refused(tmp_path):
    _assert_missing_field_refused(tmp_path, "width")


def test_missing_load_is_refused(tmp_path):
    _assert_missing_field_refused(tmp_path, "load")


def test_name_only_entry_is_refused(tmp_path):
    # The exact shape of the original bug: an entry with only a name.
    interface = """\
version: 2
signals:
  - name: osc_out
  - name: osc_en
    direction: d2a
    level: cmos_3v3
    domain: clk_sys
    width: 1
    load: {cap_ff: 4000}
"""
    ws = make_ws(tmp_path, interface=interface)
    try:
        check_split.run(["--workspace", str(ws)])
        assert False, "expected CheckError"
    except CheckError:
        pass


# ---------------------------------------------------------------- ua pads
# A Tiny Tapeout analog pad goes out through interface.yaml's ua_pins only
# (check_top_harden.py). Before this check a splitter that listed a pad
# (vctrl, bias_ref) as a crossing signal passed split, and nothing caught it
# before top_harden. Each case builds its own workspace.

PAD_SIGNAL = """\
  - name: vctrl
    direction: a2d
    level: cmos_3v3
    domain: clk_free
    width: 1
    load: {cap_ff: 1000, note: PLL control voltage pad}
"""


def _with_pad_signal(text: str, name: str = "vctrl") -> str:
    """text plus the pad entry (renamed to `name`) under its signal list."""
    return text + PAD_SIGNAL.replace("vctrl", name)


def _kinds(payload) -> set[str]:
    return {v["kind"] for v in payload["violations"]}


def test_pad_declared_by_analog_side_listed_as_signal_fails(tmp_path):
    # The planted ece298a shape: the analog side declares vctrl a pad, and
    # the splitter put it in signals (and both side specs) with no ua_pins.
    analog = _with_pad_signal(ANALOG_SPEC).replace(
        "interface:\n", "pads: [vctrl]\ninterface:\n", 1)
    ws = make_ws(tmp_path, interface=_with_pad_signal(INTERFACE),
                 digital=_with_pad_signal(DIGITAL_SPEC), analog=analog)
    payload, _out = check_split.run(["--workspace", str(ws)])
    assert payload["status"] == "violations"
    by_kind = {v["kind"]: v for v in payload["violations"]}
    assert set(by_kind) == {"pad_in_signals", "pad_missing_from_ua_pins"}
    assert by_kind["pad_in_signals"]["refs"] == ["vctrl"]
    assert "ua_pins" in by_kind["pad_in_signals"]["msg"]


def test_ua_pins_key_listed_as_signal_fails(tmp_path):
    interface = _with_pad_signal(INTERFACE) + "ua_pins:\n  vctrl: 0\n"
    ws = make_ws(tmp_path, interface=interface,
                 digital=_with_pad_signal(DIGITAL_SPEC),
                 analog=_with_pad_signal(ANALOG_SPEC))
    payload, _out = check_split.run(["--workspace", str(ws)])
    assert _kinds(payload) == {"pad_in_signals"}


def test_ua_named_signal_fails(tmp_path):
    ws = make_ws(tmp_path, interface=_with_pad_signal(INTERFACE, "ua_bias"),
                 digital=_with_pad_signal(DIGITAL_SPEC, "ua_bias"),
                 analog=_with_pad_signal(ANALOG_SPEC, "ua_bias"))
    payload, _out = check_split.run(["--workspace", str(ws)])
    assert _kinds(payload) == {"pad_in_signals"}


def test_ua_like_but_not_pad_names_pass(tmp_path):
    ws = make_ws(tmp_path, interface=_with_pad_signal(INTERFACE, "uart_rx"),
                 digital=_with_pad_signal(DIGITAL_SPEC, "uart_rx"),
                 analog=_with_pad_signal(ANALOG_SPEC, "uart_rx"))
    payload, _out = check_split.run(["--workspace", str(ws)])
    assert payload["status"] == "pass"


def test_declared_pad_missing_from_ua_pins_fails(tmp_path):
    analog = ANALOG_SPEC.replace("interface:\n", "pads: [vout]\n"
                                 "interface:\n", 1)
    ws = make_ws(tmp_path, analog=analog)
    payload, _out = check_split.run(["--workspace", str(ws)])
    assert _kinds(payload) == {"pad_missing_from_ua_pins"}
    assert payload["violations"][0]["refs"] == ["vout"]


def test_declared_pad_in_ua_pins_passes(tmp_path):
    analog = ANALOG_SPEC.replace("interface:\n", "pads: [vout]\n"
                                 "interface:\n", 1)
    ws = make_ws(tmp_path, analog=analog,
                 interface=INTERFACE + "ua_pins:\n  vout: 0\n")
    payload, _out = check_split.run(["--workspace", str(ws)])
    assert payload["status"] == "pass"


def test_malformed_pads_is_refused(tmp_path):
    analog = ANALOG_SPEC.replace("interface:\n", "pads: vout\n"
                                 "interface:\n", 1)
    ws = make_ws(tmp_path, analog=analog)
    try:
        check_split.run(["--workspace", str(ws)])
        assert False, "expected CheckError for a non-list 'pads'"
    except CheckError:
        pass


def test_malformed_ua_pins_is_refused(tmp_path):
    ws = make_ws(tmp_path, interface=INTERFACE + "ua_pins: [vout]\n")
    try:
        check_split.run(["--workspace", str(ws)])
        assert False, "expected CheckError for a non-mapping 'ua_pins'"
    except CheckError:
        pass


def test_corpus_rungs_pass_split():
    for rung in ("dac_tile", "sensor_counted"):
        ws = REPO / "corpus" / "msde" / rung
        payload, _out = check_split.run(["--workspace", str(ws)])
        assert payload["status"] == "pass", (rung, payload["violations"])


# --- #169: the digital side's spec/cdc.yaml against the interface ---------

NESTED_PORTS = ("  osc_out: {dir: input, width: 1}\n"
                "  osc_en: {dir: output, width: 1}\n")


def _nested_digital(ws: Path, clocks=("clk",), cdc=None,
                    ports=NESTED_PORTS) -> None:
    spec = ws / "digital" / "spec"
    spec.mkdir(parents=True)
    (spec / "spec.yaml").write_text(
        f"top: sensor_counted_digital\nports:\n{ports}clock:\n  domains: {list(clocks)}\n",
        encoding="utf-8")
    if cdc is not None:
        (spec / "cdc.yaml").write_text(cdc, encoding="utf-8")


def _cdc_kinds(payload) -> list[str]:
    return sorted(v["kind"] for v in payload["violations"])


def test_cdc_not_checked_before_the_digital_workspace(tmp_path):
    ws = make_ws(tmp_path)
    payload, _ = check_split.run(["--workspace", str(ws)])
    assert payload["status"] == "pass"
    assert payload["cdc"]["checked"] is False
    assert "digital/spec/spec.yaml" in payload["cdc"]["reason"]
    assert payload["cdc"]["async_ports"] == ["osc_out"]


def test_async_a2d_signal_missing_from_cdc_yaml_fails(tmp_path):
    ws = make_ws(tmp_path)
    _nested_digital(ws)
    payload, _ = check_split.run(["--workspace", str(ws)])
    assert _cdc_kinds(payload) == ["cdc_async_undeclared"]
    assert payload["cdc"] == {"checked": True, "file": "digital/spec/cdc.yaml",
                              "async_ports": ["osc_out"]}


def test_async_a2d_signal_given_a_clock_in_cdc_yaml_fails(tmp_path):
    ws = make_ws(tmp_path)
    _nested_digital(ws, cdc="ports: {osc_out: clk}\n")
    payload, _ = check_split.run(["--workspace", str(ws)])
    assert _cdc_kinds(payload) == ["cdc_async_undeclared"]


def test_cdc_yaml_declaring_the_async_port_passes(tmp_path):
    ws = make_ws(tmp_path)
    _nested_digital(ws, cdc="ports: {osc_out: async}\nwaivers: []\n")
    payload, _ = check_split.run(["--workspace", str(ws)])
    assert payload["status"] == "pass", payload["violations"]


def test_cdc_yaml_async_for_a_clocked_signal_contradicts(tmp_path):
    """sar_adc's cmp_out is clk_sys in the interface: async in cdc.yaml
    means the two files disagree."""
    iface = INTERFACE.replace("domain: clk_free", "domain: clk_sys")
    digital = DIGITAL_SPEC.replace("domain: clk_free", "domain: clk_sys")
    ws = make_ws(tmp_path, digital=digital,
                 analog=digital.replace("sensor_counted_digital",
                                        "sensor_counted_analog"),
                 interface=iface)
    _nested_digital(ws, cdc="ports: {osc_out: async}\n")
    payload, _ = check_split.run(["--workspace", str(ws)])
    assert _cdc_kinds(payload) == ["cdc_async_contradicts_interface"]


def test_a_clk_free_clock_root_is_not_an_async_port(tmp_path):
    """A PLL's vco_out is clk_free and a digital clock domain: a clock, not
    data to synchronise."""
    ws = make_ws(tmp_path)
    _nested_digital(ws, clocks=("clk", "osc_out"))
    payload, _ = check_split.run(["--workspace", str(ws)])
    assert payload["status"] == "pass", payload["violations"]
    assert payload["cdc"]["async_ports"] == []


def test_malformed_cdc_ports_is_refused(tmp_path):
    ws = make_ws(tmp_path)
    _nested_digital(ws, cdc="ports: [osc_out]\n")
    try:
        check_split.run(["--workspace", str(ws)])
        assert False, "expected CheckError"
    except CheckError:
        pass


def test_split_cdc_writes_the_async_ports_and_keeps_the_rest(tmp_path):
    import split_cdc
    import yaml
    ws = make_ws(tmp_path)
    _nested_digital(ws, cdc="ports: {en: clk}\nwaivers:\n  - {from: a, to: b, "
                            "reason: r}\n")
    payload, _ = split_cdc.run(["--workspace", str(ws)])
    assert payload["written"] == ["osc_out"]
    data = yaml.safe_load((ws / "digital/spec/cdc.yaml").read_text())
    assert data["ports"] == {"en": "clk", "osc_out": "async"}
    assert data["waivers"] == [{"from": "a", "to": "b", "reason": "r"}]
    assert check_split.run(["--workspace", str(ws)])[0]["status"] == "pass"
    # a second run writes nothing
    assert split_cdc.run(["--workspace", str(ws)])[0]["written"] == []


def test_split_cdc_refuses_a_conflicting_port(tmp_path):
    import split_cdc
    ws = make_ws(tmp_path)
    _nested_digital(ws, cdc="ports: {osc_out: clk}\n")
    try:
        split_cdc.run(["--workspace", str(ws)])
        assert False, "expected CheckError"
    except CheckError:
        pass


def test_split_cdc_needs_the_digital_workspace(tmp_path):
    import split_cdc
    ws = make_ws(tmp_path)
    try:
        split_cdc.run(["--workspace", str(ws)])
        assert False, "expected CheckError"
    except CheckError:
        pass


def test_corpus_sensor_counted_scratch_ws_checks_its_cdc_ports(tmp_path):
    """The rung as a workspace (nested digital/spec/ with its cdc.yaml):
    split checks the cdc ports and passes; without the file it fails."""
    import faults
    ws = faults.make_scratch_workspace(
        tmp_path, REPO / "corpus" / "msde" / "sensor_counted", "msde",
        "sensor_counted")
    payload, _ = check_split.run(["--workspace", str(ws)])
    assert payload["cdc"]["checked"] is True
    assert payload["status"] == "pass", payload["violations"]
    (ws / "digital" / "spec" / "cdc.yaml").unlink()
    payload, _ = check_split.run(["--workspace", str(ws)])
    assert _cdc_kinds(payload) == ["cdc_async_undeclared"]


def test_blocks_split_cdc_ports():
    """In-repo blocks: r2r_dac has no a2d signal, the PLL's only one
    (vco_out) is a digital clock domain - neither needs a cdc port."""
    for blk in ("r2r_dac", "pll"):
        payload, _ = check_split.run(["--workspace",
                                      str(REPO / "blocks" / blk)])
        assert payload["cdc"]["checked"] is True, blk
        assert payload["cdc"]["async_ports"] == [], blk
        assert not [v for v in payload["violations"]
                    if v["kind"].startswith("cdc_")], blk


# --- #110: interface version 2 and the nested digital ports --------------

def test_version_1_interface_fails_outside_the_released_blocks(tmp_path):
    ws = make_ws(tmp_path, interface=INTERFACE.replace("version: 2", "version: 1"))
    payload, _ = check_split.run(["--workspace", str(ws)])
    assert "interface_untyped" in {v["kind"] for v in payload["violations"]}


def test_version_1_is_kept_for_a_named_released_block(tmp_path):
    root = tmp_path / "blocks"
    root.mkdir()
    ws = make_ws(tmp_path, interface=INTERFACE.replace("version: 2", "version: 1"))
    pll = ws.rename(root / "pll")
    payload, _ = check_split.run(["--workspace", str(pll)])
    assert "interface_untyped" not in {v["kind"] for v in payload["violations"]}


def _swap_everywhere(text: str, old: str, new: str) -> str:
    assert old in text
    return text.replace(old, new)


def test_unknown_level_fails(tmp_path):
    ws = make_ws(
        tmp_path,
        interface=_swap_everywhere(INTERFACE, "cmos_3v3", "ttl_5v"),
        digital=_swap_everywhere(DIGITAL_SPEC, "cmos_3v3", "ttl_5v"),
        analog=_swap_everywhere(ANALOG_SPEC, "cmos_3v3", "ttl_5v"))
    payload, _ = check_split.run(["--workspace", str(ws)])
    assert "level_unknown" in {v["kind"] for v in payload["violations"]}


def test_string_load_fails_even_when_all_three_agree(tmp_path):
    ws = make_ws(
        tmp_path,
        interface=_swap_everywhere(INTERFACE, "{cap_ff: 2000}", "2pF"),
        digital=_swap_everywhere(DIGITAL_SPEC, "{cap_ff: 2000}", "2pF"),
        analog=_swap_everywhere(ANALOG_SPEC, "{cap_ff: 2000}", "2pF"))
    payload, _ = check_split.run(["--workspace", str(ws)])
    assert "load_untyped" in {v["kind"] for v in payload["violations"]}


@pytest.mark.parametrize("ports, kind", [
    ("  osc_out: {dir: input, width: 2}\n  osc_en: {dir: output, width: 1}\n",
     "nested_width_mismatch"),
    ("  osc_out: {dir: output, width: 1}\n  osc_en: {dir: output, width: 1}\n",
     "nested_direction_mismatch"),
    ("  osc_en: {dir: output, width: 1}\n", "nested_port_missing")])
def test_nested_digital_port_disagreement_fails(tmp_path, ports, kind):
    ws = make_ws(tmp_path)
    _nested_digital(ws, ports=ports)
    payload, _ = check_split.run(["--workspace", str(ws)])
    assert kind in {v["kind"] for v in payload["violations"]}


def test_nested_digital_spec_with_no_ports_fails(tmp_path):
    ws = make_ws(tmp_path)
    spec = ws / "digital" / "spec"
    spec.mkdir(parents=True)
    (spec / "spec.yaml").write_text("top: x\nclock:\n  domains: [clk]\n",
                                    encoding="utf-8")
    payload, _ = check_split.run(["--workspace", str(ws)])
    assert "nested_ports_missing" in {v["kind"] for v in payload["violations"]}
