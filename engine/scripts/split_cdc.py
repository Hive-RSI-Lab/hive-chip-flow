#!/usr/bin/env python
"""split_cdc.py - write the digital side's spec/cdc.yaml ports from the
split interface (#169).

    split_cdc.py --workspace DIR [--out FILE]

DIR is an msde block. Each interface.yaml signal check_split.async_ports
names (a2d, domain clk_free, not a digital clock domain) is written into
DIR/digital/spec/cdc.yaml's `ports` as `async`, so the digital side's cdc
gate checks its synchroniser instead of taking it as synchronous to the
primary clock. Everything else in the file (its other ports, its waivers)
is kept. A port the file already gives another domain is refused (exit 2):
the two disagree and one of them is wrong, which a person settles, not
this script. Needs the nested digital workspace (digital/spec/spec.yaml),
whose clock domains say which a2d signals are clocks rather than data.

JSON out: {"script", "status": "pass", "file", "written": [...], "ports"};
exit 0, or 2 with a remediation.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPTS.parent / "lib"))
sys.path.insert(0, str(SCRIPTS))
import checklib  # noqa: E402
from checklib import CheckError  # noqa: E402

import yaml  # noqa: E402

import check_split  # noqa: E402

SCRIPT = "split_cdc"


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--workspace", required=True, help="msde block workspace")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)

    ws = Path(args.workspace)
    spec = ws / check_split.DIGITAL_NESTED_SPEC
    if not spec.is_file():
        raise CheckError(f"no {check_split.DIGITAL_NESTED_SPEC}: create the "
                         "nested digital workspace first")
    interface = check_split.load_interface(ws / check_split.INTERFACE_REL)
    want = check_split.async_ports(
        interface, check_split.digital_clock_domains(spec))

    path = ws / check_split.DIGITAL_CDC
    data = {}
    if path.is_file():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            raise CheckError(f"{path} must be a YAML mapping")
    ports = check_split.load_cdc_ports(path)
    clash = sorted(p for p in want if p in ports and ports[p] != "async")
    if clash:
        raise CheckError(
            f"{check_split.DIGITAL_CDC} gives {', '.join(clash)} a clock "
            "domain but interface.yaml says clk_free (async): settle which "
            "is right, then fix that file")
    written = sorted(p for p in want if p not in ports)
    if written:
        data["ports"] = {**ports, **{p: "async" for p in written}}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(data, sort_keys=True),
                        encoding="utf-8")
    payload = checklib.stamp({"script": SCRIPT, "status": "pass",
                              "file": check_split.DIGITAL_CDC,
                              "written": written,
                              "ports": data.get("ports") or {}}, ws)
    return payload, args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
