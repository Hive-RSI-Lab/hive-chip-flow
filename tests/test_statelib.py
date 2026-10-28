"""engine/lib/statelib.py: normalizers, the invalidation map, two-layer gate
freshness. Hermetic (pure venv: yaml)."""
from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ENGINE = REPO / "engine"
sys.path.insert(0, str(ENGINE / "scripts"))
sys.path.insert(0, str(ENGINE / "lib"))

import pytest  # noqa: E402

import statelib  # noqa: E402


def _h(tmp_path: Path, name: str, content: str, norm: str):
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return statelib.hash_artifact(p, norm)


def test_text_eol_ignores_crlf_churn(tmp_path):
    a = _h(tmp_path, "a.v", "module x;\nendmodule\n", "text_eol")
    b = _h(tmp_path, "b.v", "module x;\r\nendmodule\r\n", "text_eol")
    assert a == b
    assert a.startswith("text_eol:")
    c = _h(tmp_path, "c.v", "module y;\nendmodule\n", "text_eol")
    assert c != a


def test_json_canonical_ignores_key_order_and_yaml_vs_json(tmp_path):
    a = _h(tmp_path, "a.json", '{"b": 1, "a": {"y": 2, "x": 3}}',
           "json_canonical")
    b = _h(tmp_path, "b.yaml", "a:\n  x: 3\n  y: 2\nb: 1\n", "json_canonical")
    assert a == b
    c = _h(tmp_path, "c.json", '{"b": 1, "a": {"y": 2, "x": 4}}',
           "json_canonical")
    assert c != a


def _harden_config(ws: Path, pdk: str, period: int = 20) -> Path:
    """A generated harden/config.json as check_harden.py writes it: absolute
    paths into the block, into chip-flow and into the PDK."""
    import json
    home = Path(statelib.__file__).resolve().parents[2]
    p = ws / "harden" / "config.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({
        "VERILOG_FILES": [f"{ws}/rtl/top.v", f"{ws}/harden/tt_um_top.v"],
        "FP_DEF_TEMPLATE": f"{home}/engine/reference/tt/x.def",
        "PDK_ROOT": pdk, "STD_CELL_LIBRARY_DIR": f"{pdk}/gf180mcuD/libs.ref",
        "CLOCK_PERIOD": period}), encoding="utf-8")
    return p


def test_json_canonical_sans_roots_reads_a_moved_block_as_the_same_config(tmp_path):
    """The block hardened elsewhere (another checkout, machine, toolchain
    path or a scratch copy) hashes the same; any other change still moves
    it, a path to somewhere else included."""
    a = _harden_config(tmp_path / "here" / "blk", "/opt/a/foss/pdks")
    b = _harden_config(tmp_path / "there" / "blk", "/home/x/.cc/foss/pdks")
    h = statelib.hash_artifact(a, "json_canonical_sans_roots")
    # written on another machine and copied here as it is: its paths name a
    # block and a chip-flow that aren't where the file now sits
    moved = tmp_path / "copied" / "blk" / "harden"
    moved.mkdir(parents=True)
    (moved / "config.json").write_text(
        b.read_text().replace(str(Path(statelib.__file__).resolve().parents[2]),
                              "/home/x/.claude/skills/chip-flow"))
    assert "/home/x/.claude" in (moved / "config.json").read_text()
    assert statelib.hash_artifact(moved / "config.json",
                                  "json_canonical_sans_roots") == h
    assert h.startswith("json_canonical_sans_roots:")
    assert h == statelib.hash_artifact(b, "json_canonical_sans_roots")
    assert statelib.hash_artifact(a, "json_canonical") != \
        statelib.hash_artifact(b, "json_canonical")
    c = _harden_config(tmp_path / "else" / "blk", "/opt/a/foss/pdks", period=25)
    assert statelib.hash_artifact(c, "json_canonical_sans_roots") != h
    import json
    doc = json.loads(a.read_text())
    doc["VERILOG_FILES"][0] = "/elsewhere/rtl/top.v"
    a.write_text(json.dumps(doc))
    assert statelib.hash_artifact(a, "json_canonical_sans_roots") != h


def _gds(stamp: int, box_x: int = 2000) -> bytes:
    """A minimal GDSII stream: BGNLIB and BGNSTR dated `stamp`, one box."""
    def rec(rtype: int, dtype: int, body: bytes = b"") -> bytes:
        return (4 + len(body)).to_bytes(2, "big") + bytes([rtype, dtype]) + body
    date = b"".join(((stamp + i) % 30000).to_bytes(2, "big") for i in range(12))
    xy = b"".join(v.to_bytes(4, "big", signed=True)
                  for v in (0, 0, box_x, 0, box_x, 2000, 0, 2000, 0, 0))
    return (rec(0x00, 0x02, (600).to_bytes(2, "big")) + rec(0x01, 0x02, date)
            + rec(0x02, 0x06, b"LIB\0") + rec(0x05, 0x02, date)
            + rec(0x06, 0x06, b"TOP\0") + rec(0x08, 0x00)
            + rec(0x0D, 0x02, (34).to_bytes(2, "big"))
            + rec(0x0E, 0x02, (0).to_bytes(2, "big")) + rec(0x10, 0x03, xy)
            + rec(0x11, 0x00) + rec(0x07, 0x00) + rec(0x04, 0x00))


def _hardened(ws: Path, pdk: str, stamp: int, box_x: int = 2000,
              cell: str = "and2") -> Path:
    """A harden/ dir as LibreLane leaves one: the generated config, a final/
    with GDS, netlist, SDF, SPEF and magic views stamped with the run's time
    and paths, a step dir and logs."""
    _harden_config(ws, pdk)
    final = ws / "harden" / "runs" / "run" / "final"
    for d in ("gds", "nl", "sdf/nom_tt", "spef/nom", "mag", "odb", "json_h"):
        (final / d).mkdir(parents=True)
    (final / "gds" / "top.gds").write_bytes(_gds(stamp, box_x))
    (final / "nl" / "top.nl.v").write_text(f"module top; {cell} u0(); endmodule\n")
    (final / "sdf" / "nom_tt" / "top.sdf").write_text(
        f'(DELAYFILE\n (SDFVERSION "3.0")\n (DATE "{stamp}")\n (DESIGN "top")\n)\n')
    (final / "spef" / "nom" / "top.spef").write_text(
        f'*SPEF "ieee 1481-1999"\n*DATE "{stamp}"\n*DESIGN "top"\n')
    (final / "mag" / "top.mag").write_text(
        f"magic\ntech gf180mcuD\nmagscale 1 10\ntimestamp {stamp}\n")
    (final / "odb" / "top.odb").write_bytes(str(stamp).encode())
    (final / "json_h" / "top.h.json").write_text(f'{{"src": "{ws}/rtl/top.v"}}')
    (final / "metrics.json").write_text(f'{{"runtime": {stamp}}}')
    step = ws / "harden" / "runs" / "run" / "01-verilator-lint"
    step.mkdir()
    (step / "flow.log").write_text(f"ran at {stamp} in {ws}\n")
    (ws / "harden" / "runs" / "run" / "flow.log").write_text(f"{stamp}\n")
    return ws / "harden"


def test_harden_signoff_reads_an_unchanged_reharden_elsewhere_as_the_same(tmp_path):
    a = _hardened(tmp_path / "box" / "blk", "/home/x/.cc/foss/pdks", 1791879499)
    b = _hardened(tmp_path / "hive" / "blk", "/opt/eda/foss/pdks", 1793216598)
    h = statelib.hash_artifact(a, "harden_signoff")
    assert h.startswith("harden_signoff:")
    assert h == statelib.hash_artifact(b, "harden_signoff")
    assert statelib.hash_artifact(a, "dir_text") != \
        statelib.hash_artifact(b, "dir_text")


@pytest.mark.parametrize("change", ["geometry", "netlist", "config", "wrapper"])
def test_harden_signoff_moves_on_any_real_change(tmp_path, change):
    a = _hardened(tmp_path / "a" / "blk", "/opt/eda/foss/pdks", 1)
    kw = {"geometry": {"box_x": 2400}, "netlist": {"cell": "or2"}}.get(change, {})
    b = _hardened(tmp_path / "b" / "blk", "/opt/eda/foss/pdks", 1, **kw)
    if change == "config":
        _harden_config(tmp_path / "c" / "blk", "/opt/eda/foss/pdks", period=25)
        (b / "config.json").write_text(
            (tmp_path / "c" / "blk" / "harden" / "config.json").read_text()
            .replace(str(tmp_path / "c"), str(tmp_path / "b")))
    if change == "wrapper":
        (b / "tt_um_top.v").write_text("module tt_um_top; endmodule\n")
    assert statelib.hash_artifact(a, "harden_signoff") != \
        statelib.hash_artifact(b, "harden_signoff")


def test_gds_sans_timestamps_refuses_what_is_not_gds():
    with pytest.raises(ValueError):
        statelib._gds_sans_timestamps(b"\x00\x02\x00\x02junk")


def test_dir_text_hashes_every_file_name_sorted(tmp_path):
    d = tmp_path / "tb"
    d.mkdir()
    (d / "a.py").write_text("x = 1\n", encoding="utf-8")
    (d / "b.py").write_text("y = 2\r\n", encoding="utf-8")
    h1 = statelib.hash_artifact(d, "dir_text")
    # CRLF churn inside a member file is invisible
    (d / "b.py").write_text("y = 2\n", encoding="utf-8")
    h2 = statelib.hash_artifact(d, "dir_text")
    assert h1 == h2
    (d / "c.py").write_text("z = 3\n", encoding="utf-8")
    h3 = statelib.hash_artifact(d, "dir_text")
    assert h3 != h1


def test_dir_text_ignores_pycache(tmp_path):
    # tb/ and holdout/ are Python packages cocotb imports directly - just
    # running the tests once (no source edit) writes __pycache__/*.pyc under
    # the very directory being hashed. That must never move the hash, or a
    # gate's recorded input goes stale on its own bytecode cache.
    d = tmp_path / "tb"
    d.mkdir()
    (d / "test_a.py").write_text("x = 1\n", encoding="utf-8")
    h1 = statelib.hash_artifact(d, "dir_text")
    cache = d / "__pycache__"
    cache.mkdir()
    (cache / "test_a.cpython-312.pyc").write_bytes(b"\x00fake bytecode")
    (d / "stray.pyc").write_bytes(b"\x00also fake")
    h2 = statelib.hash_artifact(d, "dir_text")
    assert h1 == h2


def test_missing_file_hashes_none(tmp_path):
    assert statelib.hash_artifact(tmp_path / "nope.v", "text_eol") is None


def test_unparsable_json_falls_back_to_raw(tmp_path):
    p = tmp_path / "broken.json"
    p.write_text("{not json", encoding="utf-8")
    h = statelib.hash_artifact(p, "json_canonical")
    assert h.startswith("raw:")


def test_norm_for_path(tmp_path):
    assert statelib.norm_for_path(tmp_path / "x.json") == "json_canonical"
    assert statelib.norm_for_path(tmp_path / "x.yaml") == "json_canonical"
    assert statelib.norm_for_path(tmp_path / "x.v") == "text_eol"
    (tmp_path / "d").mkdir()
    assert statelib.norm_for_path(tmp_path / "d") == "dir_text"


def test_load_map_is_valid_and_skill_scoped():
    imap = statelib.load_map()
    assert set(imap["gate_inputs"]) == set(statelib.SKILLS)
    assert set(imap["edit_classes"]) == set(statelib.SKILLS)
    assert "rtl_edit" in imap["edit_classes"]["vde"]
    assert "spec_lint" in imap["gate_inputs"]["vde"]


def test_load_map_rejects_unknown_skill(tmp_path):
    bad = tmp_path / "invalidation.yaml"
    bad.write_text("""
artifact_kinds:
  x: {path: "x", norm: text_eol}
gate_inputs:
  notaskill: {g: [x]}
edit_classes:
  vde: {}
""", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown skills"):
        statelib.load_map(bad)


def test_load_map_rejects_edit_class_gate_not_in_gate_inputs(tmp_path):
    bad = tmp_path / "invalidation.yaml"
    bad.write_text("""
artifact_kinds:
  x: {path: "x", norm: text_eol}
gate_inputs:
  vde: {g1: [x]}
edit_classes:
  vde:
    c1: {mutates: [x], stale_artifacts: [], gates: [g2], human_hold: 0}
""", encoding="utf-8")
    with pytest.raises(ValueError, match="unknown gates"):
        statelib.load_map(bad)


def test_gate_freshness_two_layer():
    entry = {"last": {"inputs": {"rtl": "dir_text:aaa"}}}
    fresh = statelib.gate_freshness(entry, {"rtl": "dir_text:aaa"})
    assert fresh == {"hash_valid": True, "changed_inputs": [],
                     "stale_marks": [], "fresh": True}
    stale_hash = statelib.gate_freshness(entry, {"rtl": "dir_text:bbb"})
    assert stale_hash["hash_valid"] is False
    assert stale_hash["fresh"] is False
    marked = dict(entry, stale=[{"human_hold": 1}])
    stale_mark = statelib.gate_freshness(marked, {"rtl": "dir_text:aaa"})
    assert stale_mark["hash_valid"] is True
    assert stale_mark["fresh"] is False  # hash matches but a mark is present


def test_gate_freshness_unknown_gate_never_fresh():
    verdict = statelib.gate_freshness({}, {})
    assert verdict["hash_valid"] is None
    assert verdict["fresh"] is False


def test_kind_path_registry_override_requires_matching_kind():
    imap = statelib.load_map()
    # no override: the default template
    assert statelib.kind_path("rtl", imap, None) == "rtl"
    # an override registered under a DIFFERENT kind must not be honored
    reg = {"rtl": {"path": "somewhere/else", "kind": "tb"}}
    assert statelib.kind_path("rtl", imap, reg) == "rtl"
    # a matching-kind override IS honored
    reg2 = {"rtl": {"path": "somewhere/else", "kind": "rtl"}}
    assert statelib.kind_path("rtl", imap, reg2) == "somewhere/else"
