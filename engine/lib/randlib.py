"""Constrained-random stimulus for cocotb benches in tb/ and holdout/ (#83).

Every helper draws from Python's `random`, which cocotb seeds from the
gate's pinned seed (cocotblib.sim_seed), so a draw replays exactly with the
gate's `--seed`, and spec `sim.seeds: K` runs K more draws. Never seed or
construct your own `random.Random` in a bench: that draw would be the same
under every seed and hide the failures the extra seeds exist to find.

    from randlib import chance, weighted, bounded, fields

    if chance(0.3): ...                       # a write on 30% of cycles
    op = weighted({"read": 3, "write": 1})    # 3:1 reads
    n = bounded(1, 16)                        # inclusive range
    txn = fields(addr=(0, 255), data=(0, 0xFF), last={True: 1, False: 7})
"""
from __future__ import annotations

import random


def chance(p: float) -> bool:
    """True with probability p (0 <= p <= 1)."""
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"chance needs 0 <= p <= 1, got {p!r}")
    return random.random() < p


def bounded(lo: int, hi: int, edge: float = 0.0) -> int:
    """An int in [lo, hi], inclusive. With `edge` > 0, that fraction of
    draws lands on lo or hi themselves, where off-by-one bugs live."""
    if lo > hi:
        raise ValueError(f"bounded needs lo <= hi, got {lo}..{hi}")
    if edge and random.random() < edge:
        return random.choice((lo, hi))
    return random.randint(lo, hi)


def weighted(choices: dict):
    """One key of `choices`, picked in proportion to its (non-negative)
    weight."""
    keys = list(choices)
    weights = [choices[k] for k in keys]
    if not keys or any(w < 0 for w in weights) or sum(weights) <= 0:
        raise ValueError(f"weighted needs non-negative weights with a "
                         f"positive sum, got {choices!r}")
    return random.choices(keys, weights=weights)[0]


def fields(**spec) -> dict:
    """A random transaction: each keyword is a field, given as a (lo, hi)
    range (bounded) or a {value: weight} dict (weighted)."""
    out = {}
    for name, rule in spec.items():
        if isinstance(rule, tuple) and len(rule) == 2:
            out[name] = bounded(*rule)
        elif isinstance(rule, dict):
            out[name] = weighted(rule)
        else:
            raise ValueError(f"field {name!r} needs a (lo, hi) range or a "
                             f"{{value: weight}} dict, got {rule!r}")
    return out
