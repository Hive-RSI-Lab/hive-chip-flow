"""Per-device transient stress for the pex_sim gate's opt-in stage.

Every MOS device and MOS capacitor of the extracted netlist is read from a
transient run: the gate pairs (Vgs, Vgd, Vgb, a MOS capacitor's Vcap) against
the DRM's DC limit and the allowance in engine/reference/stress_limits.yaml,
and the junction pairs (Vds, Vsb, Vdb) against breakdown.

A gate-pair excursion is the contiguous time |V| is over the limit. One that
peaks less than `floor_v` over it does not count. An excursion that touches a
declared repeating window is held to the repeating bounds there, per cycle of
the window; one that is not wholly inside a window is also held to the one-off
bounds with its whole length. One still over when the run ends is a failure:
nothing shows it ever ends.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import yaml

from checklib import CheckError

LIMITS_FILE = Path(__file__).resolve().parents[1] / "reference" / "stress_limits.yaml"
INST = "xdut"
MOS_PAIRS = (("Vgs", 1, 2), ("Vgd", 1, 0), ("Vgb", 1, 3))
JUNCTION_PAIRS = (("Vds", 0, 2), ("Vsb", 2, 3), ("Vdb", 0, 3))
DEVICE_MODEL = re.compile(r"^(?:[np]fet_|cap_[np]mos_)", re.I)
GROUND = ("0", "gnd")
CHUNK_ROWS = 4096


@dataclass(frozen=True)
class Window:
    name: str
    start: float
    stop: float
    period: float

    @property
    def cycles(self) -> int:
        return int(round((self.stop - self.start) / self.period))


@dataclass(frozen=True)
class Excursion:
    start: float
    stop: float
    peak: float
    t_peak: float
    open_end: bool

    @property
    def length(self) -> float:
        return self.stop - self.start


def load_limits(path: Path = LIMITS_FILE) -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def find_sidecar(ws: Path, block: str) -> Path | None:
    side = ws / "layout_ref" / f"{block}_stress.yaml"
    return side if side.is_file() else None


def _number(raw, what: str) -> float:
    # PyYAML reads an exponent with no decimal point (8e-08) as a string.
    if isinstance(raw, str):
        try:
            raw = float(raw)
        except ValueError:
            pass
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or raw != raw:
        raise CheckError(f"{what} must be a number, not {raw!r}")
    return float(raw)


def load_sidecar(path: Path) -> list[dict]:
    """The decks of a block's stress sidecar, each bench resolved beside it
    and each repeating window a whole number of cycles."""
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CheckError(f"{path.name} is not valid YAML: {exc}") from exc
    decks = (data or {}).get("decks") if isinstance(data, dict) else None
    if not isinstance(decks, list) or not decks:
        raise CheckError(f"{path.name} has no non-empty 'decks' list: a stage "
                         "with nothing to run is a refusal")
    out, seen = [], set()
    for row in decks:
        if not isinstance(row, dict) or not row.get("name") or not row.get("bench"):
            raise CheckError(f"{path.name}: every deck needs a name and a "
                             f"bench, not {row!r}")
        name = str(row["name"])
        if name in seen or not re.fullmatch(r"[A-Za-z0-9_-]+", name):
            raise CheckError(f"{path.name}: deck name {name!r} is repeated or "
                             "is not letters, digits, _ or -")
        seen.add(name)
        rel = Path(str(row["bench"]))
        if rel.is_absolute() or ".." in rel.parts:
            raise CheckError(f"{path.name}: bench {rel} must sit beside the "
                             "sidecar")
        bench = path.parent / rel
        if not bench.is_file():
            raise CheckError(f"{path.name}: deck {name} has no bench {rel}")
        corners = row.get("corners")
        if not isinstance(corners, list) or not corners:
            raise CheckError(f"{path.name}: deck {name} lists no corners")
        windows = [_window(w, name, path.name) for w in row.get("repeating") or []]
        timeout = row.get("timeout_s")
        out.append({"name": name, "bench": bench,
                    "corners": [str(c) for c in corners], "windows": windows,
                    "timeout": None if timeout is None
                    else _number(timeout, f"{name} timeout_s")})
    return out


def _window(raw, deck: str, file: str) -> Window:
    if not isinstance(raw, dict):
        raise CheckError(f"{file}: deck {deck}: a repeating window must be a "
                         f"map, not {raw!r}")
    what = f"{file}: deck {deck} window {raw.get('name')!r}"
    win = Window(str(raw.get("name")), _number(raw.get("start_s"), f"{what} start_s"),
                 _number(raw.get("stop_s"), f"{what} stop_s"),
                 _number(raw.get("period_s"), f"{what} period_s"))
    if not 0 <= win.start < win.stop or win.period <= 0:
        raise CheckError(f"{what}: needs 0 <= start < stop and a positive period")
    span = (win.stop - win.start) / win.period
    if win.cycles < 1 or abs(span - win.cycles) > 1e-3:
        raise CheckError(f"{what} is {span:.4g} periods long: it must hold a "
                         "whole number, or the cycle figures mean nothing")
    return win


def logical_lines(text: str) -> list[str]:
    """Lines with `+` continuations joined and comments dropped."""
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith("+") and out:
            out[-1] += " " + line[1:]
        elif line.strip() and not line.lstrip().startswith("*"):
            out.append(line)
    return out


def parse_devices(netlist: str, cell: str) -> tuple[list[dict], dict]:
    """([{name, model, nodes}], {other model: count}) of the X lines in
    `cell`'s .subckt: the MOS devices and MOS caps, and what was left out."""
    devices, others, inside = [], {}, False
    for line in logical_lines(netlist):
        tok = line.split()
        head = tok[0].lower()
        if head == ".subckt":
            inside = tok[1].lower() == cell.lower()
            continue
        if head == ".ends":
            inside = False
            continue
        if not inside or not head.startswith("x"):
            continue
        plain = [t for t in tok[1:] if "=" not in t]
        if len(plain) < 2:
            continue
        model = plain[-1].lower()
        if DEVICE_MODEL.match(model):
            devices.append({"name": tok[0], "model": model, "nodes": plain[:-1]})
        else:
            others[model] = others.get(model, 0) + 1
    return devices, others


def subckt_pins(netlist: str, cell: str) -> list[str]:
    for line in logical_lines(netlist):
        tok = line.split()
        if tok[0].lower() == ".subckt" and tok[1].lower() == cell.lower():
            return [t for t in tok[2:] if "=" not in t]
    raise CheckError(f"the extracted netlist has no .subckt {cell}")


def check_flat(netlist: str, cell: str, others: dict) -> None:
    """A device inside a nested subcircuit would go unscored, so a netlist
    that instantiates one of its own subcircuits is refused."""
    defined = {line.split()[1].lower() for line in logical_lines(netlist)
               if line.split()[0].lower() == ".subckt"}
    nested = sorted((defined - {cell.lower()}) & set(others))
    if nested:
        raise CheckError(f"{cell} instantiates {', '.join(nested)} from its "
                         "own netlist: the stress stage scores a flat "
                         "extraction only")


def _base(node: str) -> str:
    return re.sub(r"\.t\d+$", "", node)


def label_devices(devices: list[dict], ref_text: str, ref_cell: str) -> None:
    """Name each extracted device after its schematic device, matched by
    model and connections; failing that, by what it is connected to."""
    ref, _ = parse_devices(ref_text, ref_cell)
    table = {}
    for dev in ref:
        if len(dev["nodes"]) == 4:
            x, gate, y, _b = (n.lower() for n in dev["nodes"])
            table[(dev["model"], gate, frozenset((x, y)))] = dev["name"]
    for dev in devices:
        if len(dev["nodes"]) == 4:
            x, gate, y, _b = (_base(n).lower() for n in dev["nodes"])
            dev["label"] = table.get(
                (dev["model"], gate, frozenset((x, y))),
                f"{dev['name']} ({dev['model'][:4]} d={x} g={gate} s={y})")
        else:
            dev["label"] = f"{dev['name']} (cap {_base(dev['nodes'][0])})"


def check_models(devices: list[dict], limits: dict, where: str) -> None:
    if not devices:
        raise CheckError(f"{where} has no MOS device or MOS capacitor: a "
                         "stage with nothing to score is a refusal")
    for dev in devices:
        want = limits["models"].get(dev["model"])
        if want is None:
            raise CheckError(f"{dev['name']} is a {dev['model']}, which "
                             "stress_limits.yaml has no limit for: add the "
                             "class with its own citation")
        if len(dev["nodes"]) != (4 if want["kind"] == "mos" else 2):
            raise CheckError(f"{dev['name']} ({dev['model']}) has "
                             f"{len(dev['nodes'])} terminals: {dev['nodes']}")


def dut_of(text: str, what: str) -> tuple[list[str], str]:
    """(outer nodes, cell) of a bench's xdut instance."""
    for line in logical_lines(text):
        tok = line.split()
        if tok[0].lower() == INST:
            nodes = [t for t in tok[1:] if "=" not in t]
            if len(nodes) >= 2:
                return nodes[:-1], nodes[-1]
    raise CheckError(f"{what} has no `{INST} <nodes> <subckt>` line")


def vectors_of(devices: list[dict], pins: list[str], outer: list[str]) -> None:
    """Set each device's `vectors`: the raw-file name of each terminal, or
    None for the ground node."""
    pin_map = {p.lower(): n.lower() for p, n in zip(pins, outer)}
    for dev in devices:
        vecs = []
        for node in dev["nodes"]:
            low = node.lower()
            name = pin_map.get(low, f"{INST}.{low}")
            vecs.append(None if name in GROUND else name)
        dev["vectors"] = vecs


def with_control(bench: str, raw_name: str, vectors: list[str], what: str) -> str:
    """The bench with the terminals saved and written as a binary raw file.
    The bench's own `.tran` sets the run."""
    if re.search(r"^\s*\.control\b", bench, re.M | re.I):
        raise CheckError(f"{what} has a .control block: leave it out, the "
                         "gate adds the output it reads")
    if not re.search(r"^\s*\.tran\b", bench, re.M | re.I):
        raise CheckError(f"{what} has no .tran line: it sets the run")
    saves = "".join("save " + " ".join(f"v({v})" for v in vectors[i:i + 30]) + "\n"
                    for i in range(0, len(vectors), 30))
    tail = (".control\nset noaskquit\nset filetype=binary\n" + saves
            + f"run\nwrite {raw_name}\nquit\n.endc\n")
    ends = list(re.finditer(r"^\s*\.end\s*$", bench, re.M | re.I))
    if not ends:
        return bench.rstrip("\n") + "\n" + tail + ".end\n"
    return bench[:ends[-1].start()] + tail + bench[ends[-1].start():]


def read_binary_raw(path: Path) -> tuple[np.ndarray, dict]:
    """(times, {vector name: column}) of an ngspice binary raw, mapped and
    not loaded: a run saves every terminal of a macro."""
    size = Path(path).stat().st_size
    with open(path, "rb") as fh:
        head = b""
        while b"Binary:\n" not in head:
            block = fh.read(1 << 20)
            if not block:
                raise CheckError(f"{Path(path).name} is not an ngspice binary "
                                 "raw file (no Binary: section)")
            head += block
    offset = head.index(b"Binary:\n") + len(b"Binary:\n")
    lines = head[:offset].decode(errors="replace").splitlines()
    n_var, n_pts, names, in_vars = None, None, [], False
    for line in lines:
        if line.startswith("No. Variables:"):
            n_var = int(line.split()[-1])
        elif line.startswith("No. Points:"):
            n_pts = int(line.split()[-1])
        elif line.startswith("Variables:"):
            in_vars = True
        elif line.startswith("Binary:"):
            in_vars = False
        elif in_vars:
            names.append(line.split()[1].lower())
    if not n_var or not n_pts or len(names) != n_var:
        raise CheckError(f"{Path(path).name}: raw header is incomplete")
    if size - offset != n_pts * n_var * 8:
        raise CheckError(f"{Path(path).name} is {size - offset} data bytes, "
                         f"not the {n_pts} x {n_var} real points its header "
                         "states")
    data = np.memmap(path, dtype="<f8", mode="r", offset=offset,
                     shape=(n_pts, n_var))
    columns = {}
    for k, name in enumerate(names):
        columns[name[2:-1] if name.startswith("v(") else name] = k
    if "time" not in columns:
        raise CheckError(f"{Path(path).name} has no time vector")
    t = np.asarray(data[:, columns["time"]])
    return t, {"data": data, "columns": columns}


def _cross(t0: float, m0: float, t1: float, m1: float, level: float) -> float:
    return t0 if m1 == m0 else t0 + (level - m0) / (m1 - m0) * (t1 - t0)


def excursions(t: np.ndarray, mag: np.ndarray, limit: float) -> list[Excursion]:
    """Each contiguous time `mag` is above `limit`, ends interpolated."""
    above = mag > limit
    if not above.any():
        return []
    steps = np.diff(above.astype(np.int8))
    starts = list(np.flatnonzero(steps == 1) + 1)
    stops = list(np.flatnonzero(steps == -1) + 1)
    if above[0]:
        starts.insert(0, 0)
    if above[-1]:
        stops.append(len(mag))
    out = []
    for s, e in zip(starts, stops):
        begin = float(t[0]) if s == 0 else _cross(
            float(t[s - 1]), float(mag[s - 1]), float(t[s]), float(mag[s]), limit)
        open_end = e == len(mag)
        end = float(t[-1]) if open_end else _cross(
            float(t[e - 1]), float(mag[e - 1]), float(t[e]), float(mag[e]), limit)
        k = s + int(np.argmax(mag[s:e]))
        out.append(Excursion(begin, end, float(mag[k]), float(t[k]), open_end))
    return out


def cycle_mean(t: np.ndarray, mag: np.ndarray, a: float, b: float) -> float:
    """Time average of `mag` over [a, b], with the ends interpolated."""
    lo, hi = np.searchsorted(t, a, "right"), np.searchsorted(t, b, "left")
    tt = np.concatenate(([a], t[lo:hi], [b]))
    mm = np.concatenate(([np.interp(a, t, mag)], mag[lo:hi], [np.interp(b, t, mag)]))
    return float(np.trapezoid(mm, tt) / (b - a))


def _share_in(events: list[Excursion], a: float, b: float) -> float:
    return sum(max(0.0, min(e.stop, b) - max(e.start, a)) for e in events) / (b - a)


def _window_row(t, mag, win: Window, events: list[Excursion], dc: float,
                bounds: dict) -> tuple[dict, list[str]]:
    """The worst cycle of one window, and the bounds it breaks. The share
    counts only time more than `share_band_v` over `dc`; the share over `dc`
    itself is kept so a pass that rests on the band can be listed."""
    band = bounds["share_band_v"]
    counted = [e for w in (excursions(t, mag, dc + band),) for e in w
               if e.start < win.stop and e.stop > win.start]
    worst_share, worst_raw, worst_mean, worst_cycle = -1.0, 0.0, -1.0, 0
    for k in range(win.cycles):
        a, b = win.start + k * win.period, win.start + (k + 1) * win.period
        share = _share_in(counted, a, b)
        if share > worst_share:
            worst_share, worst_cycle = share, k
        worst_raw = max(worst_raw, _share_in(events, a, b))
        worst_mean = max(worst_mean, cycle_mean(t, mag, a, b))
    top = max(events, key=lambda e: e.peak)
    limit = bounds["time_over_share"]
    row = {"kind": "repeating", "window": win.name, "peak_v": top.peak,
           "peak_over_v": top.peak - dc, "t_peak_s": top.t_peak,
           "share": worst_share, "share_over_dc": worst_raw,
           "worst_cycle": worst_cycle, "cycle_mean_v": worst_mean,
           "band_only": worst_raw > limit >= worst_share}
    bad = []
    if top.peak - dc > bounds["peak_over_v"]:
        bad.append(f"repeating peak {1e3 * (top.peak - dc):.1f} mV over in "
                   f"window {win.name}, bound {1e3 * bounds['peak_over_v']:.0f} mV")
    if worst_share > limit:
        bad.append(f"more than {1e3 * band:g} mV over the limit for "
                   f"{100 * worst_share:.2f}% of a cycle in window {win.name}, "
                   f"bound {100 * limit:g}%")
    if worst_mean >= dc:
        bad.append(f"cycle average {worst_mean:.4f} V in window {win.name}, "
                   f"must stay under {dc:g} V")
    return row, bad


def score_gate(t: np.ndarray, wave: np.ndarray, windows: list[Window],
               limits: dict) -> dict | None:
    """None when the pair stays within the DC limit, else {status, rows,
    reasons}: `allowance` when the bounds hold, `fail` when one breaks."""
    ox = limits["gate_oxide"]
    dc, mag = ox["dc_limit_v"], np.abs(wave)
    events = [e for e in excursions(t, mag, dc) if e.peak - dc >= ox["floor_v"]]
    if not events:
        return None
    rows, reasons = [], []
    for win in windows:
        mine = [e for e in events if e.start < win.stop and e.stop > win.start]
        if mine:
            row, bad = _window_row(t, mag, win, mine, dc, ox["repeating"])
            rows.append(row)
            reasons += bad
    inside = [any(e.start >= w.start and e.stop <= w.stop for w in windows)
              for e in events]
    one_off = ox["one_off"]
    edges = [e for e, within in zip(events, inside) if not within]
    for e in edges:
        if e.open_end:
            reasons.append(f"still {1e3 * (mag[-1] - dc):.1f} mV over the "
                           f"limit when the run ends at {t[-1]:.4g} s, over "
                           f"since {e.start:.4g} s")
        if e.peak - dc > one_off["peak_over_v"]:
            reasons.append(f"one-off peak {1e3 * (e.peak - dc):.1f} mV over at "
                           f"{e.t_peak:.4g} s, bound "
                           f"{1e3 * one_off['peak_over_v']:.0f} mV")
        if e.length > one_off["time_over_s"]:
            reasons.append(f"over the limit for {1e6 * e.length:.3g} us "
                           f"from {e.start:.4g} s, bound "
                           f"{1e6 * one_off['time_over_s']:g} us")
    if edges:
        top = max(edges, key=lambda e: e.peak)
        rows.append({"kind": "one_off", "peak_v": top.peak,
                     "peak_over_v": top.peak - dc, "t_peak_s": top.t_peak,
                     "time_over_s": max(e.length for e in edges),
                     "events": len(edges)})
    return {"status": "fail" if reasons else "allowance", "rows": rows,
            "reasons": reasons}


def score_junction(t: np.ndarray, wave: np.ndarray, breakdown: float,
                   reference: float) -> dict | None:
    """None when |V| stays at or under `reference`; else its peak and time
    over it, and whether it reaches `breakdown`."""
    mag = np.abs(wave)
    if float(mag.max()) <= reference:
        return None
    k = int(np.argmax(mag))
    return {"peak_v": float(mag[k]), "t_peak_s": float(t[k]),
            "time_over_s": sum(e.length for e in excursions(t, mag, reference)),
            "breakdown_v": breakdown, "over_breakdown": float(mag[k]) > breakdown}


def _pair_list(devices: list[dict], limits: dict) -> list[dict]:
    """Every terminal pair to score, with its vectors and the level it must
    pass before its waveform is worth reading."""
    dc = limits["gate_oxide"]["dc_limit_v"]
    reference = limits["junction"]["dc_reference_v"]
    out = []
    for dev in devices:
        info = limits["models"][dev["model"]]
        vecs = dev["vectors"]
        if info["kind"] == "cap":
            sets = [("gate", (("Vcap", 0, 1),), dc)]
        else:
            sets = [("gate", MOS_PAIRS, dc), ("junction", JUNCTION_PAIRS, reference)]
        for cls, pairs, level in sets:
            out += [{"dev": dev, "pair": pair, "class": cls, "level": level,
                     "p": vecs[p], "q": vecs[q],
                     "breakdown": info.get("breakdown_v")}
                    for pair, p, q in pairs]
    return out


def _column(columns: dict, vector: str | None, ground: int) -> int:
    if vector is None:
        return ground
    if vector not in columns:
        raise CheckError(f"the stress run saved no vector {vector}")
    return columns[vector]


def _candidates(data, columns: dict, pairs: list[dict]) -> list[dict]:
    """The pairs that pass their level anywhere in the run. One pass over
    the file in row blocks: a column read per pair would rescan it thousands
    of times."""
    n_pts, n_var = data.shape
    p_col = np.array([_column(columns, r["p"], n_var) for r in pairs])
    q_col = np.array([_column(columns, r["q"], n_var) for r in pairs])
    peak = np.zeros(len(pairs))
    for i in range(0, n_pts, CHUNK_ROWS):
        rows = np.asarray(data[i:i + CHUNK_ROWS])
        block = np.column_stack((rows, np.zeros(len(rows))))
        peak = np.maximum(peak, np.abs(block[:, p_col] - block[:, q_col]).max(axis=0))
    return [r for r, top in zip(pairs, peak) if top > r["level"]]


def _load_columns(data, columns: dict, vectors: set[str]) -> dict:
    """{vector: its whole waveform}, read in one pass."""
    if not vectors:
        return {}
    names = sorted(vectors)
    index = [columns[n] for n in names]
    n_pts = data.shape[0]
    out = np.empty((n_pts, len(names)))
    for i in range(0, n_pts, CHUNK_ROWS):
        out[i:i + CHUNK_ROWS] = np.asarray(data[i:i + CHUNK_ROWS])[:, index]
    return {n: out[:, k] for k, n in enumerate(names)}


def score_run(t: np.ndarray, store: dict, devices: list[dict],
              windows: list[Window], limits: dict) -> list[dict]:
    """One finding row per device pair that left its limit in this run."""
    data, columns = store["data"], store["columns"]
    if windows and float(t[-1]) < max(w.stop for w in windows) * (1 - 1e-9):
        raise CheckError(f"the stress run stopped at {t[-1]:.4g} s, before its "
                         "last repeating window ends")
    found_pairs = _candidates(data, columns, _pair_list(devices, limits))
    waves = _load_columns(data, columns, {v for r in found_pairs
                                          for v in (r["p"], r["q"]) if v})
    reference = limits["junction"]["dc_reference_v"]
    rows = []
    for r in found_pairs:
        wave = (waves[r["p"]] if r["p"] else 0.0) - (waves[r["q"]] if r["q"] else 0.0)
        wave = wave if isinstance(wave, np.ndarray) else np.zeros(len(t))
        dev = r["dev"]
        common = {"device": dev["name"], "label": dev["label"],
                  "model": dev["model"], "pair": r["pair"], "class": r["class"]}
        if r["class"] == "gate":
            found = score_gate(t, wave, windows, limits)
        else:
            found = score_junction(t, wave, r["breakdown"], reference)
        if found:
            rows.append({**common, **found})
    return rows


def summarise(rows: list[dict], decks_run: int, devices: int,
              limits: dict, others: dict) -> dict:
    """The gate's report: every device passing only through the allowance
    with peak and time (a device failing in any run is not listed there),
    the failures, and the junction pairs over the DC reference."""
    failing = {(r["device"], r["pair"]) for r in rows
               if r["class"] == "gate" and r["status"] == "fail"}
    failed_devices = {d for d, _ in failing}
    allowance: dict = {}
    for r in rows:
        if r["class"] != "gate" or r["device"] in failed_devices:
            continue
        for row in r["rows"]:
            key = (r["device"], r["pair"], row["kind"], row.get("window"))
            have = allowance.get(key)
            if have is None or row["peak_over_v"] > have["peak_over_v"]:
                allowance[key] = {"device": r["device"], "label": r["label"],
                                  "pair": r["pair"], "corner": r["corner"],
                                  "deck": r["deck"], **row}
    junctions: dict = {}
    for r in rows:
        if r["class"] != "junction":
            continue
        key = (r["device"], r["pair"])
        if key not in junctions or r["peak_v"] > junctions[key]["peak_v"]:
            junctions[key] = {k: r[k] for k in (
                "device", "label", "pair", "corner", "deck", "peak_v",
                "t_peak_s", "time_over_s", "breakdown_v", "over_breakdown")}
    order = lambda d: (-d["peak_v"], d["device"], d["pair"])
    return {"gate_oxide_citation": limits["gate_oxide"]["citation"].strip(),
            "allowance_is_ours": limits["gate_oxide"]["own_numbers"].strip(),
            "junction_citation": limits["junction"]["citation"].strip(),
            "bounds": {k: limits["gate_oxide"][k] for k in (
                "dc_limit_v", "floor_v", "repeating", "one_off")},
            "runs": decks_run, "devices": devices,
            "other_models_not_scored": others,
            "devices_failing": sorted(failed_devices),
            "allowance_only": sorted(allowance.values(), key=order),
            "share_band_only": sorted(
                (a for a in allowance.values() if a.get("band_only")), key=order),
            "junctions_over_reference": sorted(junctions.values(), key=order)}


def failures_by_pair(rows: list[dict]) -> dict:
    """{(device, pair): [failing rows]} of the gate and junction failures."""
    out: dict = {}
    for r in rows:
        bad = r.get("status") == "fail" or r.get("over_breakdown")
        if bad:
            out.setdefault((r["device"], r["pair"]), []).append(r)
    return out
