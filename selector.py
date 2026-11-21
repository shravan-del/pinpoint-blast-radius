"""Pinpoint's Blast Radius selector.

Reads the batch in /data, decides which tests each commit could break, and
prints one JSON line per commit on stdout.  Everything else goes to stderr.

Phase 1: every test on every commit, but packed well.  No miss is possible;
the only saving over the starter is the packing.

Three rules this file must never break, because any of them rejects the run:
  * stdout holds nothing but the answer lines;
  * every commit in /data/commits gets exactly one line;
  * we never raise.  Anything unexpected falls back to running every test.
"""

from __future__ import annotations

import json
import math
import os
import sys
from pathlib import Path

# The scorer always mounts /data.  The variable exists so the unit tests and the
# local evaluation harness can point at a sample batch without Docker.
DATA = Path(os.environ.get("BLAST_RADIUS_DATA", "/data"))
MAXIMUM_WORKERS = 8

# SCORING.md: a worker costs 2 s and all arithmetic is in whole milliseconds.
WORKER_COST_MILLISECONDS = 2_000


def duration_milliseconds(seconds: float) -> int:
    """The scorer's duration for a test.  Halves round up, as in score.py."""
    return math.floor(seconds * 1000 + 0.5)


def pack(test_numbers: list[int], durations: dict[int, int]) -> list[list[int]]:
    """Split tests over the worker count that costs least.

    Longest test first onto the least loaded worker (LPT), tried for every
    worker count 1..8, keeping the cheapest by the scorer's own formula:
    the busiest worker's load plus 2 s per worker used.
    """
    if not test_numbers:
        return []

    # Ties break on the test number so the output is the same every run.
    ordered = sorted(test_numbers, key=lambda number: (-durations[number], number))

    best_workers: list[list[int]] = []
    best_cost = -1
    for worker_count in range(1, MAXIMUM_WORKERS + 1):
        workers: list[list[int]] = [[] for _ in range(worker_count)]
        loads = [0] * worker_count
        for number in ordered:
            lightest = loads.index(min(loads))
            workers[lightest].append(number)
            loads[lightest] += durations[number]

        occupied = [load for load in loads if load > 0]
        cost = max(occupied) + WORKER_COST_MILLISECONDS * len(occupied)
        if best_cost < 0 or cost < best_cost:
            best_cost = cost
            best_workers = [worker for worker in workers if worker]

        # More workers than tests cannot help, and neither can more workers
        # once one test alone already sets the busiest load.
        if worker_count >= len(ordered):
            break

    return best_workers


def select(commit_id: str, patch_text: str, all_test_numbers: list[int]) -> list[int]:
    """The tests this commit could break.  Phase 1 answers "all of them"."""
    return all_test_numbers


def main() -> int:
    try:
        tests = json.loads((DATA / "tests.json").read_text("utf-8"))["tests"]
        durations = {
            test["number"]: duration_milliseconds(test["seconds"]) for test in tests
        }
        all_test_numbers = [test["number"] for test in tests]
    except Exception as error:  # noqa: BLE001 - never crash, never print to stdout
        print(f"cannot read tests.json: {error!r}", file=sys.stderr)
        durations = {}
        all_test_numbers = []

    # The fallback selection, packed once and reused: it is the answer for every
    # commit in Phase 1, and the safe answer for any commit we cannot reason about.
    fallback = pack(all_test_numbers, durations)
    fallback_workers = json.dumps(fallback, separators=(",", ":"))

    try:
        patches = sorted((DATA / "commits").glob("*.patch"))
    except Exception as error:  # noqa: BLE001
        print(f"cannot list /data/commits: {error!r}", file=sys.stderr)
        patches = []

    out = sys.stdout
    for patch in patches:
        commit_id = patch.stem
        try:
            selected = select(commit_id, patch.read_text("utf-8", errors="replace"),
                              all_test_numbers)
            if selected is all_test_numbers:
                workers = fallback_workers
            else:
                workers = json.dumps(pack(selected, durations), separators=(",", ":"))
        except Exception as error:  # noqa: BLE001 - fail safe: run everything
            print(f"{commit_id}: falling back to every test: {error!r}", file=sys.stderr)
            workers = fallback_workers
        out.write('{"commit":' + json.dumps(commit_id) + ',"workers":' + workers + "}\n")

    print(
        f"{len(patches)} commits, {len(all_test_numbers)} tests, "
        f"{len(fallback)} workers in the fallback packing",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
