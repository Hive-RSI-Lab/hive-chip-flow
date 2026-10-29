"""bin/heavy.sh runs a command under `hive agent heavy` only inside a Hive
run whose `hive agent help` lists heavy, once per nesting; else directly.
A stub `hive` on PATH records what it was asked and runs the command."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HEAVY = REPO / "bin" / "heavy.sh"

STUB = r"""#!/bin/sh
if [ "$2" = help ]; then printf '%s\n' "$STUB_HELP"; exit 0; fi
echo "lease $*" >> "$STUB_LOG"
shift 2; [ "$1" = --what ] && shift 2; [ "$1" = -- ] && shift
exec "$@"
"""

WITH_OP = '  progress  --text "x"\n  heavy     --what "..." -- cmd   a lease'
WITHOUT_OP = '  progress  --text "x"\n  feedback  --text "heavy load"'


def run(tmp: Path, help_text: str, box: str | None, inner: str = "echo ran",
        **extra: str) -> tuple[subprocess.CompletedProcess, list[str]]:
    bindir = tmp / "bin"
    bindir.mkdir(exist_ok=True)
    (bindir / "hive").write_text(STUB)
    (bindir / "hive").chmod(0o755)
    log = tmp / "log"
    log.write_text("")
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HIVE_", "CHIP_FLOW_HEAVY"))}
    env.update(PATH=f"{bindir}:{env['PATH']}", STUB_HELP=help_text,
               STUB_LOG=str(log), **extra)
    if box is not None:
        env["HIVE_BOX"] = box
    script = f'. "{HEAVY}"; heavy_exec "a test" sh -c \'{inner}\''
    p = subprocess.run(["sh", "-c", script], env=env, capture_output=True,
                       text=True, timeout=30)
    return p, log.read_text().splitlines()


def test_leased_inside_a_run(tmp_path):
    p, log = run(tmp_path, WITH_OP, "/box", inner="echo ran; exit 3")
    assert p.returncode == 3 and p.stdout == "ran\n"
    assert log == ["lease agent heavy --what a test -- sh -c echo ran; exit 3"]


def test_direct_outside_a_run(tmp_path):
    p, log = run(tmp_path, WITH_OP, None)
    assert p.returncode == 0 and p.stdout == "ran\n" and log == []


def test_direct_when_the_op_is_missing(tmp_path):
    # "heavy" in another op's text is not the op.
    p, log = run(tmp_path, WITHOUT_OP, "/box")
    assert p.returncode == 0 and p.stdout == "ran\n" and log == []


def test_one_lease_when_nested(tmp_path):
    inner = f'. "{HEAVY}"; heavy_exec inner echo ran'
    p, log = run(tmp_path, WITH_OP, "/box", inner=inner)
    assert p.stdout == "ran\n" and len(log) == 1, log


def test_opt_out(tmp_path):
    p, log = run(tmp_path, WITH_OP, "/box", CHIP_FLOW_HEAVY="0")
    assert p.stdout == "ran\n" and log == []
