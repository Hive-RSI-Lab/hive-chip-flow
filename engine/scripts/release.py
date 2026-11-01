#!/usr/bin/env python
"""release.py - check a submission tree against its release manifest (#132).

    chip-flow release --verify DIR [--manifest FILE] [--workspace WS] [--out FILE]

Re-hashes every file release/manifest.json lists, byte for byte, and refuses
one that differs or is missing, one in gds/ lef/ src/ .github/workflows/ that
it does not list, an edited manifest, and a manifest whose release was refused
(facts.files_match still says whether the bytes agree). DIR is a submission
repo, or a block workspace (its release/tt is checked). --manifest reads the
manifest from elsewhere, for a repo that does not carry one. --workspace also
re-hashes the archived signoff views and checks the workspace's attestation:
that it still verifies and holds this manifest's digest.

JSON out; exit 0 verified, 1 findings, 2 error. Read-only.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
ENGINE = SCRIPTS.parent
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(ENGINE / "lib"))
import attest as attest_mod  # noqa: E402
import checklib  # noqa: E402
import manifestlib  # noqa: E402

SCRIPT = "release"


def run(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--verify", required=True, metavar="DIR",
                    help="submission tree, or block workspace, to check")
    ap.add_argument("--manifest", help="manifest to check against (default: "
                    f"DIR/{manifestlib.PATH})")
    ap.add_argument("--workspace", help="also check the block's archived views "
                    "and its attestation")
    ap.add_argument("--out", help="write result JSON here instead of stdout")
    args = ap.parse_args(argv)

    root = Path(args.verify)
    path = Path(args.manifest) if args.manifest else root / manifestlib.PATH
    if not path.is_file():
        raise checklib.CheckError(
            f"no manifest at {path}: run the release gate, which writes it, "
            "or name one with --manifest")
    man = checklib.load_json(path, "manifest.json")
    tree = man.get("tree")
    if not args.manifest and tree and tree != "." and (root / tree).is_dir():
        root = root / tree
    ws = Path(args.workspace) if args.workspace else None
    bad, facts = manifestlib.verify(root, man, ws)
    if ws is not None:
        verdict = attest_mod.verify(ws)
        if not verdict.get("valid"):
            bad.append(("attestation_invalid", attest_mod.CHECKS_PATH,
                        f"{attest_mod.CHECKS_PATH}: {verdict.get('reason')}"))
        else:
            sealed = (checklib.load_json(ws / attest_mod.CHECKS_PATH, "checks.json")
                      .get("tt_submission") or {}).get("manifest_sha256")
            if sealed != man.get("manifest_sha256"):
                bad.append(("attestation_invalid", attest_mod.CHECKS_PATH,
                            f"{attest_mod.CHECKS_PATH} holds manifest digest "
                            f"{sealed}, this manifest is "
                            f"{man.get('manifest_sha256')}"))
    violations = [checklib.violation("release", "error", f, None, kind, [], msg,
                                     SCRIPT) for kind, f, msg in bad]
    return checklib.report(SCRIPT, path, violations, **facts), args.out


def main(argv=None) -> int:
    return checklib.cli_wrap(SCRIPT, lambda: run(argv))


if __name__ == "__main__":
    raise SystemExit(main())
