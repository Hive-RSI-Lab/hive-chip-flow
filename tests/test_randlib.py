"""randlib: constrained-random helpers draw only from the seeded `random`."""
from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "engine" / "lib"))
import randlib  # noqa: E402


def draw():
    return [randlib.fields(a=(0, 255), op={"r": 3, "w": 1}),
            randlib.bounded(1, 16, edge=0.5), randlib.chance(0.3)]


def test_same_seed_same_draw_other_seed_other_draw():
    random.seed(5)
    a = [draw() for _ in range(20)]
    random.seed(5)
    assert [draw() for _ in range(20)] == a
    random.seed(6)
    assert [draw() for _ in range(20)] != a


def test_ranges_weights_and_edges_hold():
    random.seed(1)
    vals = [randlib.bounded(3, 9) for _ in range(2000)]
    assert min(vals) == 3 and max(vals) == 9
    edges = [randlib.bounded(0, 10**6, edge=1.0) for _ in range(50)]
    assert set(edges) == {0, 10**6}
    picks = [randlib.weighted({"x": 3, "y": 1, "z": 0}) for _ in range(4000)]
    assert "z" not in picks and 2.5 < picks.count("x") / picks.count("y") < 3.5
    assert not any(randlib.chance(0.0) for _ in range(100))
    assert all(randlib.chance(1.0) for _ in range(100))


@pytest.mark.parametrize("call", [
    lambda: randlib.chance(1.5), lambda: randlib.bounded(4, 3),
    lambda: randlib.weighted({}), lambda: randlib.weighted({"a": -1}),
    lambda: randlib.fields(a=[0, 1])])
def test_bad_constraints_are_errors(call):
    with pytest.raises(ValueError):
        call()
