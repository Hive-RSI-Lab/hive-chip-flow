"""wavelib.py - read a failing sim's waves as text (#93).

A `test_failed` finding used to carry only the assertion's message, so the
rtl fixer had to re-run the sim and open a waveform viewer to see what the
design did. check_sim now re-runs just the failing tests with FST on,
converts the dump with `eda fst2vcd`, and this module turns it into a small
text window: the block's ports and the signals the test reads, sampled at
each rising clock edge for the N cycles up to the failure time.

    parse_vcd(path, keep)        header + value changes for the kept names
    block_scope(vcd, ports)      the scope that holds the block's ports
    window(vcd, scope, clock, signals, at_ps, cycles)
                                 rows of {"t_ns", signal: value}
    format_window(win)           the rows as a fixed-width table
    fst_to_vcd(fst, vcd)         `eda fst2vcd`, streamed to a file
    signature(file, line, msg)   assert location + numbers-masked message

A sample at an edge is the value the signal held just BEFORE that edge (the
value a flop clocked by it would capture), so a register that wraps early
reads 253, 254, 0 on three edges in a row. The last row is the failure time
itself, after every change at that instant.

pyvcd is not used here (the engine reads only its own deps); the tests
cross-check this reader against it.
"""
from __future__ import annotations

import bisect
import re
import subprocess
from pathlib import Path

EDA_BIN = Path(__file__).resolve().parents[2] / "bin" / "eda"
FST2VCD_WALL_S = 300

_UNIT_PS = {"s": 10 ** 12, "ms": 10 ** 9, "us": 10 ** 6, "ns": 10 ** 3,
            "ps": 1, "fs": 10 ** -3}


class VCD:
    """A parsed VCD: timescale in ps, every var's full dotted path and its
    width, and the change lists of the ids that were kept."""

    def __init__(self):
        self.timescale_ps: float = 1.0
        self.vars: dict[str, tuple[str, int]] = {}   # path -> (id, width)
        self.scopes: list[str] = []                  # dotted, in file order
        self.changes: dict[str, list[tuple[int, str]]] = {}
        self.end_time: int = 0

    def scope_vars(self, scope: str) -> dict[str, str]:
        """{leaf name: full path} of the vars directly in scope."""
        pre = scope + "."
        return {p[len(pre):]: p for p in self.vars
                if p.startswith(pre) and "." not in p[len(pre):]}

    def value_at(self, path: str, t: int, before: bool = False) -> str | None:
        """The raw bit string of path at time t (raw units): after every
        change at t, or with before=True the value just before t."""
        vid = self.vars[path][0]
        ch = self.changes.get(vid) or []
        times = [c[0] for c in ch]
        i = (bisect.bisect_left(times, t) if before
             else bisect.bisect_right(times, t))
        return ch[i - 1][1] if i else None


def _parse_timescale(text: str) -> float:
    m = re.match(r"\s*(\d+)\s*([munpf]?s)\s*$", text)
    if not m:
        raise ValueError(f"unreadable $timescale {text!r}")
    return int(m.group(1)) * _UNIT_PS[m.group(2)]


def parse_vcd(path, keep=None) -> VCD:
    """Parse path. keep, if given, is a predicate on a var's full path:
    only the ids of kept vars get their changes stored (a long run's dump
    can be large; the window needs a handful of signals)."""
    vcd = VCD()
    stack: list[str] = []
    keep_ids: set[str] | None = None if keep is None else set()
    with open(path, encoding="utf-8", errors="replace") as f:
        # header
        buf: list[str] = []
        for line in f:
            buf.extend(line.split())
            while "$end" in buf:
                cut = buf.index("$end")
                tok, buf = buf[:cut], buf[cut + 1:]
                if not tok:
                    continue
                kw = tok[0]
                if kw == "$timescale":
                    vcd.timescale_ps = _parse_timescale(" ".join(tok[1:]))
                elif kw == "$scope" and len(tok) >= 3:
                    stack.append(tok[2])
                    vcd.scopes.append(".".join(stack))
                elif kw == "$upscope" and stack:
                    stack.pop()
                elif kw == "$var" and len(tok) >= 5:
                    width, vid, name = int(tok[2]), tok[3], tok[4]
                    full = ".".join(stack + [name])
                    vcd.vars.setdefault(full, (vid, width))
                    if keep_ids is not None and keep(full):
                        keep_ids.add(vid)
                elif kw == "$enddefinitions":
                    break
            else:
                continue
            break
        # value changes
        t = 0
        ch = vcd.changes
        for line in f:
            s = line.strip()
            if not s:
                continue
            c = s[0]
            if c == "#":
                t = int(s[1:])
                continue
            if c in "bBrR":
                val, _, vid = s[1:].partition(" ")
                vid = vid.strip()
            elif c in "01xXzZ":
                val, vid = c, s[1:]
            else:          # $dumpvars / $end / $comment etc.
                continue
            if keep_ids is None or vid in keep_ids:
                ch.setdefault(vid, []).append((t, val.lower()))
        vcd.end_time = t
    return vcd


def block_scope(vcd: VCD, ports) -> str | None:
    """The shallowest scope that holds every one of ports as a var: the
    pad frame's top (tt_frame_<top>) when the sim ran through it, else the
    block itself. None when no scope holds them all."""
    want = set(ports)
    for scope in sorted(vcd.scopes, key=lambda s: s.count(".")):
        if want <= set(vcd.scope_vars(scope)):
            return scope
    return None


def to_text(bits: str | None, width: int) -> str:
    """A sample as text: decimal when every bit is known, else the bits."""
    if bits is None:
        return "?"
    if 1 < width and len(bits) < width:
        # VCD drops leading bits: pad with 0 after a 0/1, else repeat the
        # x/z (`bx` on an 8-bit var is xxxxxxxx)
        bits = (bits[0] if bits[0] in "xz" else "0") * (width - len(bits)) \
            + bits
    if re.fullmatch(r"[01]+", bits):
        return str(int(bits, 2))
    return bits if width > 1 else bits[-1]


def rising_edges(vcd: VCD, clock_path: str, upto: int) -> list[int]:
    vid = vcd.vars[clock_path][0]
    out, prev = [], None
    for t, v in vcd.changes.get(vid) or []:
        if t > upto:
            break
        if v == "1" and prev is not None and prev != "1":
            out.append(t)
        prev = v
    return out


def window(vcd: VCD, scope: str, clock: str, signals, at_ps: float,
           cycles: int = 16) -> dict:
    """The last `cycles` rising edges of scope.clock at or before at_ps,
    each a row {"t_ns": edge time, name: value just before the edge}, then a
    last row at the failure time itself (value after every change there).
    signals are names in scope or full dotted paths."""
    at = int(round(at_ps / vcd.timescale_ps))
    local = vcd.scope_vars(scope)
    paths = {}
    for s in signals:
        p = local.get(s) or next((q for q in (f"{scope}.{s}", s)
                                  if q in vcd.vars), None)
        if p:
            paths[s] = p
    clk = local.get(clock) or clock
    if clk not in vcd.vars:
        raise ValueError(f"no clock {clock!r} in scope {scope!r}")
    edges = rising_edges(vcd, clk, at)[-cycles:]
    ns = vcd.timescale_ps / 1000.0

    def row(t, before):
        r = {"t_ns": round(t * ns, 3)}
        for name, p in paths.items():
            r[name] = to_text(vcd.value_at(p, t, before=before),
                              vcd.vars[p][1])
        return r

    rows = [row(t, True) for t in edges]
    last = row(at, False)
    last["at_failure"] = True
    rows.append(last)
    return {"scope": scope, "clock": clock, "signals": list(paths),
            "sampled": "just before each rising edge of the clock; the last "
                       "row is the failure time",
            "failure_ns": round(at * ns, 3), "rows": rows}


def format_window(win: dict) -> str:
    """The window as a fixed-width table, one row per edge."""
    cols = ["t_ns"] + list(win["signals"])
    def mark(r):
        # check_sim's first_divergence (#360): the first row showing an
        # output that differs from the test's last passing run
        marks = (["failure"] if r.get("at_failure") else []) + (
            ["first divergence"] if r.get("first_divergence") else [])
        return "<- " + ", ".join(marks) if marks else ""
    cells = [[str(r.get(c, "")) for c in cols] + [mark(r)]
             for r in win["rows"]]
    widths = [max(len(c), *(len(x[i]) for x in cells))
              for i, c in enumerate(cols)]
    line = lambda xs: "  ".join(x.rjust(w) for x, w in zip(xs, widths))  # noqa: E731
    out = [line(cols)]
    out += [(line(x[:-1]) + ("  " + x[-1] if x[-1] else "")).rstrip()
            for x in cells]
    return "\n".join(out)


_NUM = re.compile(r"(?<![A-Za-z_])(?:0[xX][0-9a-fA-F_]+|0[bB][01_]+|"
                  r"\d+(?:\.\d+)?(?:[eE][-+]?\d+)?)")


def mask_numbers(text: str) -> str:
    return _NUM.sub("N", text)


def signature(file: str | None, line: int | None, message: str | None) -> str:
    """`file:line: message` with every number in the message masked to N
    and its whitespace collapsed: the same assert failing in three tests at
    three different values is one signature."""
    msg = " ".join(mask_numbers(message or "").split())
    loc = f"{Path(file).name}:{line}" if file else "?"
    return f"{loc}: {msg}"


_TB_FRAME = re.compile(r'File "([^"]+)", line (\d+)')


def assert_location(traceback: str, tb_dir) -> tuple[str | None, int | None]:
    """The innermost traceback frame in a file under tb_dir: where the
    failing assert (or raise) is. (None, None) when there is none."""
    tb_dir = str(Path(tb_dir).resolve())
    hit = (None, None)
    for m in _TB_FRAME.finditer(traceback or ""):
        f = m.group(1)
        try:
            inside = str(Path(f).resolve()).startswith(tb_dir)
        except OSError:
            inside = False
        if inside:
            hit = (f, int(m.group(2)))
    return hit


def named_signals(text: str, names) -> list[str]:
    """Which of names appear in text as whole identifiers (`dut.count`,
    `count`), in names' order."""
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", text or ""))
    return [n for n in names if n in words]


def test_reads(source: str, func: str) -> list[str]:
    """The `dut.NAME` signals the test function func reads (from its own
    body: up to the next top-level def/decorator), in first-seen order."""
    m = re.search(rf"^(?:async\s+)?def\s+{re.escape(func)}\b.*?(?=^\S|\Z)",
                  source, re.M | re.S)
    body = m.group(0) if m else ""
    seen: list[str] = []
    for n in re.findall(r"\bdut\.([A-Za-z_][A-Za-z0-9_]*)", body):
        if n not in seen:
            seen.append(n)
    return seen


def fst_to_vcd(fst, vcd) -> Path:
    """Convert an FST dump to VCD with `eda fst2vcd`, streaming to vcd.
    Raises checklib.CheckError when the tool fails or hangs."""
    import checklib
    vcd = Path(vcd)
    try:
        with open(vcd, "w", encoding="utf-8") as out:
            proc = checklib.run_tool([str(EDA_BIN), "fst2vcd", str(fst)],
                                     wall=FST2VCD_WALL_S, stdout=out,
                                     stderr=subprocess.PIPE,
                                     stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise checklib.CheckError(
            f"eda fst2vcd {checklib.timeout_why(exc)}") from exc
    if proc.returncode != 0:
        err = (proc.stderr or b"").decode("utf-8", "replace")[-800:]
        raise checklib.CheckError(f"eda fst2vcd exited {proc.returncode}: "
                                  f"{err}")
    return vcd
