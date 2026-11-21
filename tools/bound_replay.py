#!/usr/bin/env python3
"""Upper bound on statement-level replay, before building it.

Generous on purpose: assume every candidate test can be proved to pass by
replaying only the touched statements, for a flat cost per test, with one
fixed cost per commit for the child process and overlay.  The exceptions are
the tests that cannot be decomposed that way - a single huge parse, or work
that only happens inside a spawned pool - which stay blind.

If the bound is not comfortably better than what we ship today, replay cannot
pay however well it is implemented.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import selector  # noqa: E402
from pinpoint import choose  # noqa: E402
from pinpoint.mapstore import MapStore, bits_to_numbers  # noqa: E402

WORKER = 2_000

# Not decomposable into independent statements we can replay cheaply:
# one enormous parse, or work that happens inside a spawned pool.
NOT_REPLAYABLE = {
    "tests/test_parser.py::TestParser::test_max_nodes",
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
    "tests/test_executor.py::TestExecutor::test_execute_tpch",
    "tests/test_executor.py::TestExecutor::test_optimized_tpch",
}


def pack_cost(numbers, durations):
    workers = selector.pack(list(numbers), durations)
    if not workers:
        return 0
    return max(sum(durations[n] for n in w) for w in workers) + WORKER * len(workers)


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--maps", type=Path, default=REPO / "maps" / "maps.zip")
    parser.add_argument("--per-test-ms", type=int, default=150)
    parser.add_argument("--per-commit-ms", type=int, default=1000)
    options = parser.parse_args(argv)

    tests = json.loads((options.data_dir / "tests.json").read_text())["tests"]
    answers = json.loads((options.data_dir / "answers.json").read_text())["commits"]
    durations = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
    name_of = {t["number"]: t["id"] for t in tests}
    store = MapStore(options.maps)
    everything = store.all_tests()

    blind_total = bound_total = cpu_total = 0
    for commit_id in sorted(answers):
        text = (options.data_dir / "commits" / f"{commit_id}.patch").read_text(
            "utf-8", errors="replace")
        try:
            bits = choose.select(text, store)
            candidates = (list(durations) if bits == everything
                          else bits_to_numbers(bits))
        except Exception:  # noqa: BLE001
            candidates = list(durations)
        failing = set(answers[commit_id]["failing"])
        blind = pack_cost(candidates, durations)
        blind_total += blind
        if not candidates:
            continue

        replayable = [n for n in candidates if name_of[n] not in NOT_REPLAYABLE]
        provable = [n for n in replayable if n not in failing]
        # Charge for replaying every replayable candidate, pass or fail.
        spent = options.per_commit_ms + options.per_test_ms * len(replayable)
        survivors = [n for n in candidates if n not in set(provable)]
        with_replay = spent + pack_cost(survivors, durations)
        if with_replay < blind:
            bound_total += with_replay
            cpu_total += spent
        else:
            bound_total += blind
    n = len(answers)
    print(f"{options.data_dir.name:10s} per-test {options.per_test_ms:4d} ms  "
          f"blind {blind_total/1000:8.3f} ({blind_total/1000/n:5.3f}/c)  "
          f"bound {bound_total/1000:8.3f} ({bound_total/1000/n:5.3f}/c)  "
          f"CPU {cpu_total/1000:7.3f}  saved {(blind_total-bound_total)/1000:+8.3f}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
