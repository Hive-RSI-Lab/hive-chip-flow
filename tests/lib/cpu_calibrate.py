"""tests/lib/cpu_calibrate.py - how fast this box runs the suite's kind of work.

    eda python tests/lib/cpu_calibrate.py [RUNS]

Runs a fixed piece of pure-Python work (dict and string churn, a sort, a
JSON round trip: what the engine suite spends its CPU on) RUNS times
(default 5) in the suite's own interpreter, and prints the fastest run's
CPU seconds (time.process_time, so the box's load does not count, only
how fast a core does the work). tests/check-engine.sh divides it by the
reference box's figure to scale its CPU budget to this box's speed.
"""
import json
import sys
import time


def work() -> int:
    d: dict[str, int] = {}
    s = 0
    for i in range(3_000_000):
        k = f"k{i % 5000}"
        d[k] = d.get(k, 0) + i
        s += len(k) * (i & 7)
    json.loads(json.dumps(sorted(d.items(), key=lambda kv: kv[1])))
    return s


def main() -> int:
    runs = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    best = None
    for _ in range(runs):
        t = time.process_time()
        work()
        dt = time.process_time() - t
        best = dt if best is None else min(best, dt)
    print(f"{best:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
