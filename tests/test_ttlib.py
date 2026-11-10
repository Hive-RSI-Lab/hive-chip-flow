"""engine/lib/ttlib.py: the TT GF180 tile pin map (docs/design.md 1.5,
"### M4."). Pure logic, no toolchain needed - pin_budget()/tile_die_area()
read the vendored template files (engine/reference/tt/) directly, and
generate_tt_wrapper()/generate_glsim_harness() are text generation."""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ENGINE = Path(__file__).resolve().parents[1] / "engine"
sys.path.insert(0, str(ENGINE / "lib"))

import pytest  # noqa: E402

import ttlib  # noqa: E402

COUNTER8_SPEC = {
    "top": "counter8",
    "ports": {
        "clk": {"dir": "input", "width": 1},
        "rst": {"dir": "input", "width": 1},
        "count": {"dir": "output", "width": 8},
    },
    "tt_pins": {"clk": "clk", "rst": "~rst_n", "count": "uo_out[7:0]"},
}


def test_pin_budget_reads_the_vendored_def_template():
    budget = ttlib.pin_budget()
    assert budget == {"ui_in": 8, "uio_in": 8, "uo_out": 8, "uio_out": 8,
                      "uio_oe": 8}


def test_tile_die_area_reads_the_vendored_tile_sizes_yaml():
    assert ttlib.tile_die_area() == "0 0 346.64 160.72"
    with pytest.raises(ttlib.TTError, match="tiles='9x9'"):
        ttlib.tile_die_area("9x9")


def test_clean_spec_validates_with_no_problems():
    assert ttlib.validate_tt_pins(COUNTER8_SPEC) == []


def test_missing_tt_pins_mapping_is_a_problem():
    spec = {**COUNTER8_SPEC, "tt_pins": {}}
    problems = ttlib.validate_tt_pins(spec)
    assert any("no non-empty 'tt_pins'" in p for p in problems)


def test_port_with_no_mapping_is_a_problem():
    spec = {**COUNTER8_SPEC, "tt_pins": {"clk": "clk", "rst": "~rst_n"}}
    problems = ttlib.validate_tt_pins(spec)
    assert any("'count' has no tt_pins mapping" in p for p in problems)


def test_wrong_width_is_a_problem():
    spec = {**COUNTER8_SPEC,
            "tt_pins": {**COUNTER8_SPEC["tt_pins"], "count": "uo_out[3:0]"}}
    problems = ttlib.validate_tt_pins(spec)
    assert any("8 bit(s) wide but tt_pins maps it to 4 bit(s)" in p
              for p in problems)


def test_direction_mismatch_is_a_problem():
    spec = {**COUNTER8_SPEC,
            "tt_pins": {**COUNTER8_SPEC["tt_pins"], "clk": "uo_out[0]"}}
    problems = ttlib.validate_tt_pins(spec)
    assert any("'input' but maps to uo_out" in p for p in problems)


def test_inverted_output_is_a_problem():
    spec = {"top": "x", "ports": {"o": {"dir": "output", "width": 1}},
            "tt_pins": {"o": "~uo_out[0]"}}
    problems = ttlib.validate_tt_pins(spec)
    assert any("cannot be inverted" in p for p in problems)


def test_out_of_range_bit_is_a_problem():
    spec = {**COUNTER8_SPEC,
            "tt_pins": {**COUNTER8_SPEC["tt_pins"], "count": "uo_out[8:1]"}}
    problems = ttlib.validate_tt_pins(spec)
    assert any("only has uo_out[7:0]" in p for p in problems)


def test_overlapping_bits_is_a_problem():
    spec = {
        "top": "x",
        "ports": {"a": {"dir": "input", "width": 1},
                 "b": {"dir": "input", "width": 1}},
        "tt_pins": {"a": "ui_in[0]", "b": "ui_in[0]"},
    }
    problems = ttlib.validate_tt_pins(spec)
    assert any("already used by another port" in p for p in problems)


def test_tt_pins_naming_an_unknown_port_is_a_problem():
    spec = {**COUNTER8_SPEC,
            "tt_pins": {**COUNTER8_SPEC["tt_pins"], "ghost": "ui_in[0]"}}
    problems = ttlib.validate_tt_pins(spec)
    assert any("names ports not in spec.yaml" in p for p in problems)


def test_bad_expression_syntax_is_a_problem():
    spec = {**COUNTER8_SPEC,
            "tt_pins": {**COUNTER8_SPEC["tt_pins"], "count": "not_a_pin"}}
    problems = ttlib.validate_tt_pins(spec)
    assert any("does not match the grammar" in p for p in problems)


def test_generate_tt_wrapper_refuses_when_tt_pins_do_not_fit():
    spec = {**COUNTER8_SPEC, "tt_pins": {"clk": "clk", "rst": "~rst_n"}}
    with pytest.raises(ttlib.TTError, match="does not fit the tile"):
        ttlib.generate_tt_wrapper(spec)


def test_generate_tt_wrapper_wires_every_port_and_ties_off_the_rest():
    v = ttlib.generate_tt_wrapper(COUNTER8_SPEC)
    assert "module tt_um_counter8 (" in v
    assert "assign w_rst = ~rst_n;" in v
    assert "counter8 u_counter8" in v
    assert "uio_oe = 8'b00000000;" in v  # nothing maps to uio_out here


def test_generate_tt_wrapper_drives_uio_oe_for_used_uio_out_bits():
    spec = {
        "top": "x",
        "ports": {"a": {"dir": "output", "width": 1}},
        "tt_pins": {"a": "uio_out[3]"},
    }
    v = ttlib.generate_tt_wrapper(spec)
    assert "uio_oe = 8'b00001000;" in v


BUS_SPEC = {
    "top": "bus",
    "ports": {"d": {"dir": "input", "width": 2},
              "q": {"dir": "output", "width": 2},
              "q_oe": {"dir": "output", "width": 1},
              "x": {"dir": "output", "width": 1}},
    "tt_pins": {"d": "uio_in[1:0]", "q": "uio_out[1:0]",
                "q_oe": "uio_oe[0]", "x": "uio_out[5]"},
}


def test_generate_tt_wrapper_lets_a_port_drive_uio_oe():
    # a tri-state bus: the design turns uio[0] around itself; uio[1] and
    # uio[5] are driven with no port on their enables, so they stay outputs
    assert ttlib.validate_tt_pins(BUS_SPEC) == []
    v = ttlib.generate_tt_wrapper(BUS_SPEC)
    assert ("assign uio_oe = {1'b0, 1'b0, 1'b1, 1'b0, 1'b0, 1'b0, 1'b1, "
            "w_q_oe};") in v
    h = ttlib.generate_glsim_harness(BUS_SPEC)
    assert "assign q_oe = tt_uio_oe[0];" in h


def test_uio_oe_with_no_uio_out_is_a_problem():
    spec = {**BUS_SPEC, "tt_pins": {**BUS_SPEC["tt_pins"], "q_oe": "uio_oe[3]"}}
    problems = ttlib.validate_tt_pins(spec)
    assert any("uio_oe[3] is driven by a port but no port drives uio_out[3]"
               in p for p in problems)


def test_uio_oe_is_an_output_bus():
    spec = {**BUS_SPEC, "tt_pins": {**BUS_SPEC["tt_pins"], "d": "uio_oe[1:0]",
                                    "q_oe": "uio_in[0]"}}
    problems = ttlib.validate_tt_pins(spec)
    assert any("'d' is 'input' but maps to uio_oe" in p for p in problems)


def test_generate_glsim_harness_is_the_reverse_wrapper():
    v = ttlib.generate_glsim_harness(COUNTER8_SPEC)
    assert "module counter8 (" in v
    assert "tt_um_counter8 dut (" in v
    assert "wire tt_rst_n = ~rst;" in v
    assert "$sdf_annotate" not in v


def test_generate_glsim_harness_adds_sdf_annotate_when_given_a_path():
    v = ttlib.generate_glsim_harness(COUNTER8_SPEC, sdf_path="/tmp/x.sdf")
    assert '$sdf_annotate("/tmp/x.sdf", dut);' in v


def test_wrapper_name_does_not_double_prefix():
    assert ttlib.wrapper_name({"top": "counter8"}) == "tt_um_counter8"
    assert ttlib.wrapper_name({"top": "tt_um_counter8"}) == "tt_um_counter8"


def test_gf180_tech_reads_the_vendored_tech_py():
    tech = ttlib.gf180_tech()
    assert tech.librelane_pdk_args == "--pdk gf180mcuD"
    assert tech.project_top_metal_layer == "Metal4"


def test_stdcell_liberty_path_resolves_from_vendored_lib_map(tmp_path):
    p = ttlib.stdcell_liberty_path("max_ss_125C_3v00", tmp_path)
    assert p == (tmp_path / "gf180mcuD" / "libs.ref" / "gf180mcu_fd_sc_mcu7t5v0" /
                "lib" / "gf180mcu_fd_sc_mcu7t5v0__ss_125C_3v00.lib")


def test_template_locked_keys_are_the_ones_below_the_do_not_change_marker():
    locked = ttlib.template_locked_keys()
    assert {"RUN_KLAYOUT_DRC", "FP_SIZING", "MAGIC_WRITE_LEF_PINONLY"} <= locked
    assert "PL_TARGET_DENSITY_PCT" not in locked  # a documented user knob


def test_harden_config_merges_override_last_and_refuses_owned_keys(tmp_path):
    spec = {**COUNTER8_SPEC, "clock": {"period_ns": 20}}
    rtl = [tmp_path / "counter8.v"]
    wrapper = tmp_path / "tt_um_counter8.v"
    config = ttlib.harden_config(spec, rtl, wrapper, tmp_path,
                                 override={"RUN_POST_GRT_RESIZER_TIMING": 1})
    assert config["RUN_POST_GRT_RESIZER_TIMING"] == 1
    forbidden = ttlib.forbidden_override_keys()
    assert {"CLOCK_PERIOD", "DIE_AREA", "VERILOG_FILES", "PDK_ROOT"} <= forbidden
    for key in ("CLOCK_PERIOD", "RUN_KLAYOUT_XOR", "LIB_SYNTH"):
        with pytest.raises(ttlib.TTError, match=key):
            ttlib.harden_config(spec, rtl, wrapper, tmp_path, override={key: 1})


def test_harden_override_may_not_replace_the_specs_macros(tmp_path):
    # spec.yaml `macros` owns macro_config()'s keys; an override merged last
    # would otherwise swap the hard macro out from under LVS.
    spec = {**COUNTER8_SPEC, "clock": {"period_ns": 20}}
    rtl = [tmp_path / "counter8.v"]
    wrapper = tmp_path / "tt_um_counter8.v"
    for key in ("MACROS", "PDN_MACRO_CONNECTIONS", "PDN_CFG",
                "MAGIC_EXT_USE_GDS", "EXTRA_SPICE_MODELS"):
        with pytest.raises(ttlib.TTError, match=key):
            ttlib.harden_config(spec, rtl, wrapper, tmp_path, override={key: 1})
    config = ttlib.harden_config(spec, rtl, wrapper, tmp_path,
                                 override={"PL_TARGET_DENSITY_PCT": 50})
    assert config["PL_TARGET_DENSITY_PCT"] == 50


def _pdk_config(tmp_path: Path, keys: dict) -> Path:
    pdk = tmp_path / "gf180mcuD"
    cfg = pdk / "libs.tech" / "librelane" / "config.tcl"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("".join(f"set ::env({k}) {v}\n" for k, v in keys.items()))
    return pdk


def test_tile_power_stripes_match_the_pdns_own_layout(tmp_path):
    """The PLL's 1x2 analog tile, as LibreLane's DEF drew it: VPWR at
    19.68 + 38.87k (the core's 6-site margin plus PDN_VOFFSET; the
    template's FP_PDN_VPITCH over the PDK's pitch), VGND 3.3 um right."""
    pdk = _pdk_config(tmp_path, {"PDN_VOFFSET": 16.32, "PDN_VWIDTH": 1.6,
                                 "PDN_VSPACING": 1.7, "PDN_VPITCH": 153.6})
    pdn = ttlib.tile_pdn(pdk)
    assert pdn["PDN_VPITCH"] == 38.87
    stripes = ttlib.tile_power_stripes("0 0 346.64 325.36", pdn)
    assert stripes[:4] == [("VPWR", 19.68), ("VGND", 22.98),
                           ("VPWR", 58.55), ("VGND", 61.85)]
    assert stripes[-2:] == [("VPWR", 330.64), ("VGND", 333.94)]
    assert len(stripes) == 18
    with pytest.raises(ttlib.TTError, match="PDN_VOFFSET"):
        ttlib.tile_pdn(_pdk_config(tmp_path / "x", {"PDN_VWIDTH": 1.6}))


def test_macro_config_sets_the_pdn_halo_placement_counts_on(tmp_path):
    macro = {"cell": "a", "instance": "u_analog", "location": [1, 2],
             "power": {"vdd": "vdd", "vss": "vss"},
             "files": {k: f"a.{k}" for k in ("gds", "lef", "vh", "spice")}}
    config = ttlib.macro_config([macro])
    assert config["PDN_HORIZONTAL_HALO"] == ttlib.MACRO_PDN_HALO_UM
    assert config["PDN_VERTICAL_HALO"] == ttlib.MACRO_PDN_HALO_UM
    assert {"PDN_VERTICAL_HALO", "PDN_VOFFSET", "FP_PDN_VWIDTH"} <= \
        ttlib.forbidden_override_keys()


def test_macro_output_nets_are_dont_touch_by_their_exact_names():
    import re
    # names as check_top_harden reads them from a real synthesized netlist
    # (tests/test_check_top_harden.py builds them from the fixtures)
    macro = {"cell": "a", "instance": "u_analog", "location": [1, 2],
             "power": {"vdd": "vdd", "vss": "vss"}, "ua": {"x": 0},
             "dont_touch_nets": {"osc_out": "u_sensor_counted_digital.osc_async",
                                 "q": "u_core.q[0]"},
             "files": {k: f"a.{k}" for k in ("gds", "lef", "vh", "spice")}}
    rx = re.compile(ttlib.macro_config([macro])["RSZ_DONT_TOUCH_RX"])
    assert rx.search("u_sensor_counted_digital.osc_async")
    assert rx.search("u_core.q[0]") and rx.search(r"u_core.q\[0\]")
    assert rx.search("ua[3]")
    # nothing guessed from the pin's name
    for other in ("osc_out", "w_osc_out", "u_sensor_counted_digital.osc_out",
                  "u_sensor_counted_digital.osc_async2", "u_core.q[1]",
                  "x.u_sensor_counted_digital.osc_async"):
        assert not rx.search(other), other
    # a clock output (not listed) stays repairable; no macro net, no key
    assert "RSZ_DONT_TOUCH_RX" not in ttlib.macro_config(
        [{**macro, "ua": {}, "dont_touch_nets": {}}])
    merged = re.compile(ttlib.merge_rx("^a$", "^b$"))
    assert merged.search("a") and merged.search("b") and not merged.search("c")


def test_clock_port_none_when_spec_names_no_domain():
    assert ttlib.clock_port(COUNTER8_SPEC) is None
    assert ttlib.clock_port({**COUNTER8_SPEC, "clock": {"period_ns": 20}}) is None


def test_clock_port_clk_for_a_clk_mapping():
    spec = {**COUNTER8_SPEC, "clock": {"period_ns": 20, "domains": ["clk"]}}
    assert ttlib.clock_port(spec) == "clk"


def test_clock_port_maps_a_spare_input_bit():
    spec = {
        "top": "divider",
        "ports": {"osc_out": {"dir": "input", "width": 1},
                 "count": {"dir": "output", "width": 8}},
        "tt_pins": {"osc_out": "ui_in[0]", "count": "uo_out[7:0]"},
        "clock": {"period_ns": 20, "domains": ["osc_out"]},
    }
    assert ttlib.clock_port(spec) == "ui_in[0]"


PLL_SPEC = {
    "top": "pll",
    "ports": {"clk": {"dir": "input", "width": 1},
              "vco_out": {"dir": "input", "width": 1},
              "lock": {"dir": "output", "width": 1}},
    "tt_pins": {"clk": "clk", "vco_out": "ui_in[3]", "lock": "uo_out[2]"},
    "clock": {"period_ns": 80, "domains": ["clk", "vco_out"],
              "primary": "clk"},
}
PLL_SDC = """\
# vco_out is the second domain
create_clock -name vco_out -period 3.0 [get_ports {ui_in[3]}]
create_generated_clock -name clk_pre -source [get_ports {ui_in[3]}] \\
    -divide_by 8 [get_pins u_pre/q2/Q]
set_clock_groups -asynchronous -group [get_clocks clk] \\
    -group [get_clocks {vco_out clk_pre}]
"""


def _pll(**clock):
    return {**PLL_SPEC, "clock": {**PLL_SPEC["clock"], **clock}}


def test_primary_domain_is_the_one_domain_or_the_named_one():
    one = {**COUNTER8_SPEC, "clock": {"period_ns": 20, "domains": ["clk"]}}
    assert ttlib.primary_domain(one) == "clk"
    assert ttlib.primary_domain(COUNTER8_SPEC) is None
    assert ttlib.primary_domain(PLL_SPEC) == "clk"
    assert ttlib.clock_port(PLL_SPEC) == "clk"
    # the explicit key beats list order: vco_out is second in the list
    assert ttlib.clock_port(_pll(primary="vco_out")) == "ui_in[3]"


def test_primary_domain_refuses_to_guess_between_several():
    spec = {**COUNTER8_SPEC,
            "clock": {"period_ns": 20, "domains": ["clk", "rst"]}}
    with pytest.raises(ttlib.TTError, match="clock.primary is not set"):
        ttlib.clock_port(spec)


def test_primary_domain_must_be_one_of_the_domains():
    with pytest.raises(ttlib.TTError, match="not one of"):
        ttlib.clock_port(_pll(primary="clk_pre"))
    one = {**COUNTER8_SPEC,
           "clock": {"period_ns": 20, "domains": ["clk"], "primary": "rst"}}
    with pytest.raises(ttlib.TTError, match="not one of"):
        ttlib.clock_port(one)


def test_design_sdc_problems_pass_a_good_sdc():
    assert ttlib.design_sdc_problems(PLL_SPEC, PLL_SDC) == []
    # a domain may be clocked by its port alone, under any name
    by_port = "create_clock -name vco -period 3 [get_ports ui_in\\[3\\]]\n"
    assert ttlib.design_sdc_problems(PLL_SPEC, by_port) == []


def test_design_sdc_problems_refuse_redefining_the_primary():
    for line in ("create_clock -name clk -period 800 [get_ports clk]",
                 "create_clock -name slow -period 800 [get_ports {clk}]",
                 "create_generated_clock -name clk -source "
                 "[get_ports {ui_in[3]}] -divide_by 2 [get_pins a/Q]"):
        problems = ttlib.design_sdc_problems(PLL_SPEC, PLL_SDC + line + "\n")
        assert len(problems) == 1 and "primary clock" in problems[0], line
    # a generated clock whose -source is the primary port is not a redefinition
    ok = PLL_SDC + ("create_generated_clock -name half -source "
                    "[get_ports clk] -divide_by 2 [get_pins d/Q]\n")
    assert ttlib.design_sdc_problems(PLL_SPEC, ok) == []


def test_design_sdc_problems_refuse_a_domain_left_unclocked():
    sdc = "# the vco clock is commented out\n# create_clock -name vco_out\n"
    problems = ttlib.design_sdc_problems(PLL_SPEC, sdc)
    assert len(problems) == 1
    assert "'vco_out'" in problems[0] and "ui_in[3]" in problems[0]


def test_prepare_harden_sdc_refuses_several_domains_without_one(tmp_path):
    with pytest.raises(ttlib.TTError, match="does not exist"):
        ttlib.prepare_harden_sdc(PLL_SPEC, tmp_path)
    assert not (tmp_path / ttlib.FLOW_SDC_NAME).exists()


def test_prepare_harden_sdc_refuses_a_bad_design_sdc(tmp_path):
    (tmp_path / ttlib.HARDEN_SDC_NAME).write_text(
        "create_clock -name clk -period 800 [get_ports clk]\n",
        encoding="utf-8")
    with pytest.raises(ttlib.TTError, match="primary clock") as exc:
        ttlib.prepare_harden_sdc(PLL_SPEC, tmp_path)
    assert "'vco_out'" in str(exc.value)
    assert not (tmp_path / ttlib.FLOW_SDC_NAME).exists()


def test_prepare_harden_sdc_writes_the_flow_sdc(tmp_path):
    design = tmp_path / ttlib.HARDEN_SDC_NAME
    design.write_text(PLL_SDC, encoding="utf-8")
    flow = ttlib.prepare_harden_sdc(PLL_SPEC, tmp_path)
    assert flow == tmp_path / ttlib.FLOW_SDC_NAME
    lines = flow.read_text(encoding="utf-8").splitlines()
    base = lines.index("source $::env(SCRIPTS_DIR)/base.sdc")
    # the design's SDC by the design directory LibreLane runs from, never
    # by this machine's path
    assert lines.index(f'source "$::env(DESIGN_DIR)/{design.name}"') > base
    assert str(tmp_path) not in flow.read_text()
    assert "set_propagated_clock [all_clocks]" in flow.read_text()


def test_portable_config_names_no_machine_path(tmp_path):
    # harden/config.json is committed with a block: a path in it is the
    # workspace's (dir::, relative to harden/) or a copy beside it, never
    # this machine's
    ws = tmp_path / "blk"
    (ws / "rtl").mkdir(parents=True)
    harden = ws / "harden"
    harden.mkdir()
    spec = {**_analog_spec({"vout": 0}), "clock": {"period_ns": 20}}
    spec["macros"][0].update(location=[10, 10], power={"vdd": "vdd",
                             "vss": "vss"}, files={k: str(ws / "macros" / f"m.{k}")
                                                  for k in ("gds", "lef", "vh", "spice")})
    config = ttlib.harden_config(spec, [ws / "rtl" / "a.v"],
                                 harden / "tt_um_a.v", tmp_path / "pdks",
                                 flow_sdc=harden / "flow.sdc",
                                 override={"EXTRA_LEFS": ["/elsewhere/x.lef"]})
    out = ttlib.portable_config(config, harden)
    assert out["VERILOG_FILES"] == ["dir::../rtl/a.v", "dir::tt_um_a.v"]
    assert out["PNR_SDC_FILE"] == out["SIGNOFF_SDC_FILE"] == "dir::flow.sdc"
    assert out["FP_DEF_TEMPLATE"] == "dir::tt_analog_1x2.def"
    assert (harden / "tt_analog_1x2.def").read_bytes() == \
        ttlib.def_template_path("1x2", True).read_bytes()
    assert out["PDN_CFG"] == "dir::macro_pdn.tcl"
    assert (harden / "macro_pdn.tcl").read_bytes() == \
        ttlib.MACRO_PDN_CFG.read_bytes()
    macro = next(iter(out["MACROS"].values()))
    assert macro["gds"] == ["dir::../macros/m.gds"]
    assert out["EXTRA_SPICE_MODELS"] == ["dir::../macros/m.spice"]
    assert "PDK_ROOT" not in out  # check_harden passes --pdk-root
    assert out["EXTRA_LEFS"] == ["/elsewhere/x.lef"]  # not ours to move
    assert str(tmp_path) not in json.dumps(out)


def test_prepare_harden_sdc_one_clock_without_a_design_sdc_is_unchanged(tmp_path):
    spec = {**COUNTER8_SPEC, "clock": {"period_ns": 20, "domains": ["clk"]}}
    (tmp_path / ttlib.FLOW_SDC_NAME).write_text("stale\n", encoding="utf-8")
    assert ttlib.prepare_harden_sdc(spec, tmp_path) is None
    assert not (tmp_path / ttlib.FLOW_SDC_NAME).exists()
    config = ttlib.harden_config(spec, [tmp_path / "counter8.v"],
                                 tmp_path / "tt_um_counter8.v", tmp_path)
    assert "PNR_SDC_FILE" not in config and "SIGNOFF_SDC_FILE" not in config


def test_harden_config_points_both_sdc_keys_at_the_flow_sdc(tmp_path):
    flow = tmp_path / "flow.sdc"
    config = ttlib.harden_config(PLL_SPEC, [tmp_path / "pll.v"],
                                 tmp_path / "tt_um_pll.v", tmp_path,
                                 flow_sdc=flow)
    assert config["CLOCK_PORT"] == "clk"
    assert config["CLOCK_PERIOD"] == 80.0
    assert config["PNR_SDC_FILE"] == config["SIGNOFF_SDC_FILE"] == str(flow)
    with pytest.raises(ttlib.TTError, match="no design SDC"):
        ttlib.harden_config(PLL_SPEC, [tmp_path / "pll.v"],
                            tmp_path / "tt_um_pll.v", tmp_path)


def test_override_may_not_set_the_sdc_keys(tmp_path):
    for key in ("PNR_SDC_FILE", "SIGNOFF_SDC_FILE"):
        path = tmp_path / "config.override.json"
        path.write_text(f'{{"{key}": "x.sdc"}}', encoding="utf-8")
        with pytest.raises(ttlib.TTError, match="constraints.sdc"):
            ttlib.load_harden_override(path)


def test_clock_port_raises_on_a_multi_bit_or_output_mapping():
    multi = {
        "top": "x",
        "ports": {"c": {"dir": "input", "width": 2},
                 "o": {"dir": "output", "width": 1}},
        "tt_pins": {"c": "ui_in[1:0]", "o": "uo_out[0]"},
        "clock": {"period_ns": 20, "domains": ["c"]},
    }
    with pytest.raises(ttlib.TTError, match="clk or one ui_in/uio_in bit"):
        ttlib.clock_port(multi)
    out = {**multi, "clock": {"period_ns": 20, "domains": ["o"]}}
    with pytest.raises(ttlib.TTError, match="clk or one ui_in/uio_in bit"):
        ttlib.clock_port(out)


def test_harden_config_sets_clock_port_from_a_spare_input_bit(tmp_path):
    spec = {
        "top": "divider",
        "ports": {"osc_out": {"dir": "input", "width": 1},
                 "count": {"dir": "output", "width": 8}},
        "tt_pins": {"osc_out": "ui_in[0]", "count": "uo_out[7:0]"},
        "clock": {"period_ns": 20, "domains": ["osc_out"]},
    }
    config = ttlib.harden_config(spec, [tmp_path / "divider.v"],
                                 tmp_path / "tt_um_divider.v", tmp_path)
    assert config["CLOCK_PORT"] == "ui_in[0]"


def test_harden_config_sets_clock_port_clk_for_a_clk_mapping(tmp_path):
    spec = {**COUNTER8_SPEC, "clock": {"period_ns": 20, "domains": ["clk"]}}
    config = ttlib.harden_config(spec, [tmp_path / "counter8.v"],
                                 tmp_path / "tt_um_counter8.v", tmp_path)
    assert config["CLOCK_PORT"] == "clk"


def test_harden_config_leaves_the_template_default_clock_port_with_no_domain(tmp_path):
    config = ttlib.harden_config(COUNTER8_SPEC, [tmp_path / "counter8.v"],
                                 tmp_path / "tt_um_counter8.v", tmp_path)
    assert config["CLOCK_PORT"] == "clk"  # the vendored template's default


def test_load_harden_override_refuses_clock_port(tmp_path):
    path = tmp_path / "config.override.json"
    path.write_text('{"CLOCK_PORT": "ui_in[0]"}', encoding="utf-8")
    with pytest.raises(ttlib.TTError, match="CLOCK_PORT"):
        ttlib.load_harden_override(path)
    assert "CLOCK_PORT" in ttlib.forbidden_override_keys()


def test_load_harden_override_absent_is_empty_and_bad_json_is_refused(tmp_path):
    assert ttlib.load_harden_override(tmp_path / "config.override.json") == {}
    bad = tmp_path / "config.override.json"
    bad.write_text("{not json", encoding="utf-8")
    with pytest.raises(ttlib.TTError, match="not valid JSON"):
        ttlib.load_harden_override(bad)


# ---------------------------------------------------------------------------
# analog tile (tt-analog-tile): macro pins sent to the ua pads
# ---------------------------------------------------------------------------

def _analog_spec(ua):
    return {"top": "t", "ports": {"a": {"dir": "input", "width": 1},
                                  "b": {"dir": "output", "width": 1}},
            "tt_pins": {"a": "ui_in[0]"},
            "macros": [{"cell": "c", "instance": "u_analog",
                        "pins": {"b": "b"}, "ua": ua}]}


def test_analog_template_values_come_from_the_vendored_files():
    assert ttlib.analog_default_tiles() == "1x2"
    assert ttlib.analog_pin_limit() == 6
    assert ttlib.pin_budget("1x2", analog=True)["ua"] == 8
    assert "ua" not in ttlib.pin_budget()


def test_ua_pins_make_the_spec_an_analog_tile():
    spec = _analog_spec({"vout": 0})
    assert ttlib.is_analog(spec) and ttlib.spec_tiles(spec) == "1x2"
    assert ttlib.validate_tt_pins(spec) == []
    assert ttlib.def_template_path("1x2", analog=True).is_file()
    digital = _analog_spec({})
    assert not ttlib.is_analog(digital)
    assert ttlib.spec_tiles(digital) == ttlib.DEFAULT_TILES


def test_wrapper_wires_ua_pins_and_digital_wrapper_has_no_ua():
    text = ttlib.generate_tt_wrapper(_analog_spec({"vout": 0}))
    assert "inout  wire [7:0] ua," in text
    assert ".vout(ua[0])" in text and ".b(w_b)" in text
    assert "ua" not in ttlib.generate_tt_wrapper(_analog_spec({})).split(
        "u_analog")[0]


def test_ua_gap_duplicate_and_overflow_are_problems():
    gap = ttlib.validate_tt_pins(_analog_spec({"vout": 1}))
    assert any("no gap" in p for p in gap)
    spec = _analog_spec({"vout": 0})
    spec["macros"].append({"cell": "d", "instance": "u2", "pins": {},
                           "ua": {"x": 0}})
    assert any("claimed by both" in p for p in ttlib.validate_tt_pins(spec))
    many = _analog_spec({f"p{k}": k for k in range(7)})
    assert any("allows 6" in p for p in ttlib.validate_tt_pins(many))
    bound = _analog_spec({"b": 0})
    assert any("may not also go to a ua pad" in p
               for p in ttlib.validate_tt_pins(bound))


def test_no_analog_template_for_a_1x1_tile():
    spec = _analog_spec({"vout": 0})
    spec["tiles"] = "1x1"
    assert any("no vendored DEF template" in p
               for p in ttlib.validate_tt_pins(spec))


def test_harden_config_uses_the_analog_def(tmp_path):
    spec = _analog_spec({"vout": 0})
    spec["macros"][0].update(location=[10, 10], power={"vdd": "vdd",
                             "vss": "vss"}, files={k: "/x" for k in
                                                  ("gds", "lef", "vh", "spice")})
    cfg = ttlib.harden_config(spec, [], tmp_path / "w.v", tmp_path)
    assert cfg["FP_DEF_TEMPLATE"].endswith("analog/tt_analog_1x2.def")
    assert cfg["DIE_AREA"] == ttlib.tile_die_area("1x2")
    # the resizer leaves the pad nets alone: post-GRT repair_design stopped
    # on ua[0] with RSZ-0074, and a buffer there would sit in the analog path
    rx = re.compile(cfg["RSZ_DONT_TOUCH_RX"])
    assert all(rx.search(f"ua[{k}]") for k in range(8))
    assert not any(rx.search(n) for n in ("uo_out[0]", "ua", "u_ua[0]",
                                          "ua[0]_buf"))


def test_a_digital_tile_touches_every_net(tmp_path):
    spec = _analog_spec({})
    spec["macros"][0].update(location=[10, 10], power={"vdd": "vdd",
                             "vss": "vss"}, files={k: "/x" for k in
                                                  ("gds", "lef", "vh", "spice")})
    cfg = ttlib.harden_config(spec, [], tmp_path / "w.v", tmp_path)
    assert "RSZ_DONT_TOUCH_RX" not in cfg


def test_info_yaml_claims_exactly_the_used_ua_pins(tmp_path):
    import yaml
    ttlib.write_info_yaml(_analog_spec({"vout": 0, "vref": 1}),
                          tmp_path / "info.yaml")
    data = yaml.safe_load((tmp_path / "info.yaml").read_text())
    assert data["project"]["analog_pins"] == 2
    assert data["project"]["tiles"] == "1x2"
    assert data["pinout"] == {"ua[0]": "vout", "ua[1]": "vref"}
    ttlib.write_info_yaml(_analog_spec({}), tmp_path / "d.yaml")
    digital = yaml.safe_load((tmp_path / "d.yaml").read_text())
    assert "analog_pins" not in digital["project"]
    assert digital["project"]["tiles"] == ttlib.DEFAULT_TILES


def test_harden_config_repairs_to_the_limits_timing_signs_off(tmp_path):
    # Regression: a 37-flop block hardened with the PDK defaults failed the
    # timing gate on max cap at the CTS root (8 clkbuf_16 trunk inputs) and
    # on ss max slew after routing (post-GRT repair off). The generated
    # config must keep both repairs on; they come after the tech layer so a
    # template or tech default cannot turn them back off.
    config = ttlib.harden_config(COUNTER8_SPEC, [tmp_path / "counter8.v"],
                                 tmp_path / "tt_um_counter8.v", tmp_path)
    assert config["CTS_ROOT_BUFFER"] == "gf180mcu_fd_sc_mcu7t5v0__clkbuf_8"
    assert config["RUN_POST_GRT_DESIGN_REPAIR"] is True
    # the timing limits themselves stay the PDK's: nothing here loosens them
    for key in ("MAX_CAPACITANCE_CONSTRAINT", "MAX_TRANSITION_CONSTRAINT",
                "MAX_FANOUT_CONSTRAINT"):
        assert key not in config


def _powered_spec():
    spec = _analog_spec({"vout": 0})
    spec["macros"][0]["power"] = {"vdd": "vdd", "vss": "vss"}
    return spec


def test_project_carries_the_template_power_and_the_hardened_wrapper_does_not():
    # #165: TT's analog template declares VGND/VDPWR on src/project.v; the
    # hardened wrapper leaves them to LibreLane (VPWR/VGND on the tile)
    spec = _powered_spec()
    hard = ttlib.generate_tt_wrapper(spec)
    proj = ttlib.generate_tt_project(spec)
    assert "VGND" not in hard and "VDPWR" not in hard
    head = proj.split(");")[0]
    assert "input  wire       VGND," in head and "input  wire       VDPWR," in head
    assert ("`ifdef USE_POWER_PINS\n      .vdd(VDPWR),\n      .vss(VGND),\n"
            "`endif\n      .b(w_b)") in proj
    # the same logic: only the power lines differ
    extra = [ln for ln in proj.splitlines() if ln not in hard.splitlines()]
    assert extra == ["    input  wire       VGND,", "    input  wire       VDPWR,",
                     "`ifdef USE_POWER_PINS", "      .vdd(VDPWR),",
                     "      .vss(VGND),", "`endif"]


def test_project_of_a_macro_with_no_power_has_no_ties():
    proj = ttlib.generate_tt_project(_analog_spec({"vout": 0}))
    assert "VDPWR," in proj and "USE_POWER_PINS" not in proj


@pytest.mark.slow
@pytest.mark.skipif(not shutil.which("yosys"), reason="needs the eda image's yosys")
def test_project_reads_as_tt_precheck_and_a_powered_sim_read_it(tmp_path):
    # precheck's verilog_syntax_check is one `read_verilog -sv` with no
    # defines; a GL/powered read defines USE_POWER_PINS
    (tmp_path / "project.v").write_text(ttlib.generate_tt_project(_powered_spec()))
    (tmp_path / "c.v").write_text(
        "module c(\n`ifdef USE_POWER_PINS\n inout vdd, inout vss,\n`endif\n"
        " input b, inout vout);\nendmodule\n"
        "module t(input a, output b);\nassign b = a;\nendmodule\n")
    for defs in ([], ["-DUSE_POWER_PINS"]):
        r = subprocess.run(["yosys", "-q", "-p",
                            f"read_verilog -sv {' '.join(defs)} project.v c.v; "
                            "hierarchy -check -top tt_um_t"],
                           cwd=tmp_path, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr + r.stdout


# the pad reset synchroniser (docs/design.md, TT tile section)
CLOCKED_COUNTER8 = {**COUNTER8_SPEC, "clock": {"domains": ["clk"]}}


def test_pad_reset_latency_is_the_flow_constant_for_a_clocked_reset():
    assert ttlib.pad_reset_latency(CLOCKED_COUNTER8) == ttlib.PAD_RESET_LATENCY == 2
    # no clock domain to sample on, or nothing reads the pad: no synchroniser
    assert ttlib.pad_reset_latency(COUNTER8_SPEC) == 0
    no_rst = {**CLOCKED_COUNTER8, "tt_pins": {"clk": "clk", "rst": "ui_in[0]",
                                              "count": "uo_out[7:0]"}}
    assert ttlib.pad_reset_latency(no_rst) == 0


def test_wrapper_feeds_the_block_reset_through_named_sync_flops():
    v = ttlib.generate_tt_wrapper(CLOCKED_COUNTER8)
    assert "reg rst_n_sync_q1;" in v and "reg rst_n_sync_q2;" in v
    # async assert, sync release: rst_n low clears both, the release runs
    # through the chain
    assert "always @(posedge clk or negedge rst_n) begin" in v
    assert "rst_n_sync_q1 <= 1'b0;" in v and "rst_n_sync_q2 <= 1'b0;" in v
    assert "rst_n_sync_q1 <= 1'b1;" in v
    assert "rst_n_sync_q2 <= rst_n_sync_q1;" in v
    assert "assign w_rst = ~rst_n_sync_q2;" in v
    assert "~rst_n;" not in v  # never straight from the pin
    assert "| rst_n" not in v and "& rst_n" not in v  # nor ORed with it


def test_harness_publishes_the_latency_and_can_be_renamed():
    v = ttlib.generate_glsim_harness(CLOCKED_COUNTER8, module_name="tt_frame_counter8")
    assert "module tt_frame_counter8" in v
    assert f"wire [7:0] {ttlib.PAD_RESET_LATENCY_NET} = 8'd2;" in v
    v0 = ttlib.generate_glsim_harness(COUNTER8_SPEC)
    assert "module counter8" in v0
    assert f"wire [7:0] {ttlib.PAD_RESET_LATENCY_NET} = 8'd0;" in v0


def test_write_sim_frame_writes_wrapper_and_harness(tmp_path):
    sources, top = ttlib.write_sim_frame(CLOCKED_COUNTER8, tmp_path / "frame")
    assert top == "tt_frame_counter8"
    assert sorted(p.name for p in sources) == ["tt_frame_counter8.v",
                                               "tt_um_counter8.v"]
    assert all(p.is_file() for p in sources)


def test_write_sim_frame_none_without_a_synchroniser(tmp_path):
    assert ttlib.write_sim_frame(COUNTER8_SPEC, tmp_path / "a") is None
    # a tile with a hard macro is framed too (#208), with the macro's
    # pin-only model among the sources
    macro = {**CLOCKED_COUNTER8, "macros": [{"cell": "m", "instance": "u_m",
                                             "pins": {}}]}
    sources, _top = ttlib.write_sim_frame(macro, tmp_path / "b")
    assert sources[-1].name == "m.v"
    assert ttlib.write_sim_frame(CLOCKED_COUNTER8, tmp_path / "d") is not None


# the declared opt-out (spec.yaml pad_reset_sync, docs/design.md)
OPTED_OUT = {**CLOCKED_COUNTER8,
             "pad_reset_sync": {"mode": False, "reason": "tb holds clk; see #1"}}


def test_opt_out_without_a_reason_refuses_by_name(tmp_path):
    for cfg in ({"mode": False}, {"mode": "off", "reason": "  "},
                {"mode": False, "reason": None}):
        spec = {**CLOCKED_COUNTER8, "pad_reset_sync": cfg}
        for call in (lambda: ttlib.pad_reset_latency(spec),
                     lambda: ttlib.generate_tt_wrapper(spec),
                     lambda: ttlib.write_sim_frame(spec, tmp_path / "f"),
                     lambda: ttlib.pad_reset_facts(spec)):
            with pytest.raises(ttlib.PadResetOptOutError,
                               match="^pad_reset_sync_no_reason: "):
                call()


def test_opt_out_malformed_refuses():
    for cfg in ("off", {"mode": True, "reason": "x"},
                {"mode": "on", "reason": "x"},
                {"mode": False, "reason": "x", "latency": 1}):
        spec = {**CLOCKED_COUNTER8, "pad_reset_sync": cfg}
        with pytest.raises(ttlib.PadResetOptOutError,
                           match="^pad_reset_sync_malformed: "):
            ttlib.pad_reset_latency(spec)


def test_opt_out_passes_rst_n_straight_through_and_says_so(tmp_path):
    assert ttlib.pad_reset_opt_out(OPTED_OUT) == "tb holds clk; see #1"
    assert ttlib.pad_reset_opt_out(CLOCKED_COUNTER8) is None
    assert ttlib.pad_reset_latency(OPTED_OUT) == 0
    v = ttlib.generate_tt_wrapper(OPTED_OUT)
    assert "rst_n_sync_q1" not in v and "rst_n_sync_q2" not in v
    assert "assign w_rst = ~rst_n;" in v
    assert "pad reset synchroniser off (spec.yaml pad_reset_sync): " \
           "tb holds clk; see #1" in v
    assert ttlib.write_sim_frame(OPTED_OUT, tmp_path / "f") is None
    assert ttlib.pad_reset_facts(OPTED_OUT) == {
        "latency": 0, "sync": False, "opt_out_reason": "tb holds clk; see #1"}
    assert ttlib.pad_reset_facts(CLOCKED_COUNTER8) == {
        "latency": 2, "sync": True, "opt_out_reason": None}


def test_macro_bound_output_may_also_go_to_a_uo_pad_but_an_input_may_not():
    spec = _analog_spec({"vout": 0})
    spec["tt_pins"]["b"] = "uo_out[3]"
    assert ttlib.validate_tt_pins(spec) == []
    text = ttlib.generate_tt_wrapper(spec)
    assert ".b(w_b)" in text and "w_b," in text.split("assign uo_out")[1]
    spec["tt_pins"]["a"] = "ui_in[0]"
    spec["macros"][0]["pins"]["a"] = "a"
    assert any("may not also have a tt_pins" in p
               for p in ttlib.validate_tt_pins(spec))


def test_pad_true_signal_keeps_its_tt_pin_in_the_macro_top_spec():
    iface = {"signals": [{"name": "b", "direction": "d2a", "pad": True},
                         {"name": "c", "direction": "d2a"},
                         {"name": "d", "direction": "a2d", "pad": True}]}
    pads = ttlib.interface_pad_signals(iface)
    assert pads == {"b"}
    dspec = {"tt_pins": {"b": "uo_out[3]", "c": "uo_out[4]", "d": "ui_in[0]"}}
    tspec = ttlib.macro_top_spec(dspec, {"cell": "m"}, {"b", "c", "d"} - pads)
    assert tspec["tt_pins"] == {"b": "uo_out[3]"}
