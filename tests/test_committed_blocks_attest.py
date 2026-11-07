"""#248: every committed block attests valid from a clean checkout.

#242 and #245 re-ran cdc and lec on blocks/counter and r2r_dac's digital
side, committed the new gate records without re-running `release`, and
merged green: a clone of main then verified neither block as released.
This test runs `attest.py verify-tree` on HEAD, so the `test` job goes red
on such a commit. blocks/pll is the one known exception (attest.KNOWN_STALE,
#212, thread pll-paper-update)."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "engine" / "scripts"))
sys.path.insert(0, str(REPO / "engine" / "lib"))

import attest  # noqa: E402
import state as state_mod  # noqa: E402


def _tree(repo: Path, capsys, rev: str = "HEAD") -> tuple[int, dict]:
    code = attest.main(["verify-tree", "--repo", str(repo), "--rev", rev])
    return code, json.loads(capsys.readouterr().out)


def test_every_committed_block_attests_valid_from_a_clean_checkout(capsys):
    if not (REPO / ".git").exists():
        pytest.fail("no git checkout to verify: the committed blocks were not checked")
    code, out = _tree(REPO, capsys)
    assert code == 0, "\n".join(out["problems"])
    checked = [n for n, r in out["blocks"].items() if r["valid"] is not None]
    assert checked, "verify-tree found no committed block to check"
    assert set(out["blocks"]) - set(checked) <= set(attest.KNOWN_STALE)


def test_pll_is_the_only_known_exception_and_says_why():
    assert set(attest.KNOWN_STALE) == {"pll"}
    assert "#212" in attest.KNOWN_STALE["pll"]
    assert "pll-paper-update" in attest.KNOWN_STALE["pll"]


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t",
                    "-c", "user.email=t@t", *args], check=True,
                   capture_output=True)


def test_a_gate_rerun_committed_without_release_goes_red(tmp_path, capsys):
    """The #242/#245 plant: the counter as committed, then cdc re-run and
    the new record committed with no `release` after it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    archive = subprocess.run(
        ["git", "-C", str(REPO), "archive", "--format=tar", "HEAD",
         "--", "blocks/counter"], check=True, capture_output=True).stdout
    subprocess.run(["tar", "-x", "-C", str(repo)], input=archive, check=True)
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "counter as released")
    code, out = _tree(repo, capsys)
    assert code == 0, out["problems"]

    st = state_mod.State.load(repo / "blocks" / "counter" / "state.json")
    st.record_gate("cdc", {"status": "pass"})   # cdc re-ran and passed
    st.save()
    _git(repo, "commit", "-q", "-am", "cdc re-run, no release")

    code, out = _tree(repo, capsys)
    assert code == 1
    assert out["invalid"] == ["counter"]
    assert out["blocks"]["counter"]["stale_gates"] == ["cdc"]
    assert "counter: not released" in out["problems"][0]
    assert "cdc" in out["problems"][0]
    # the working tree is not what is checked: the committed revision is
    code, _ = _tree(repo, capsys, rev="HEAD~1")
    assert code == 0
