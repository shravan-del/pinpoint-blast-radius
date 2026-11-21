#!/usr/bin/env python3
"""Score the selector against a labeled set of commits.

Works on the sample set and on locally generated mutants alike.  Runs on the
host, no Docker: it imports the selector's own modules, so what it measures is
what the image would print.

  python3 tools/evaluate.py sample
  python3 tools/evaluate.py eval/mutants --maps maps/maps.zip
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import selector  # noqa: E402
from pinpoint import choose  # noqa: E402
from pinpoint.mapstore import MapStore, bits_to_numbers  # noqa: E402

WORKER_COST = 2_000
MISS_PENALTY = 300_000

BIG_TESTS = (
    "tests/test_parser.py::TestParser::test_max_nodes",
    "tests/test_executor.py::TestExecutor::test_execute_tpcds",
)


def cost_of(numbers: list[int], durations: dict[int, int]) -> int:
    workers = selector.pack(numbers, durations)
    if not workers:
        return 0
    loads = [sum(durations[n] for n in worker) for worker in workers]
    return max(loads) + WORKER_COST * len(loads)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path, help="holds tests.json, commits/, answers.json")
    parser.add_argument("--maps", type=Path, default=REPO / "maps" / "maps.zip")
    parser.add_argument("--show", type=int, default=12,
                        help="how many of the costliest commits to list")
    parser.add_argument("--quiet", action="store_true")
    arguments = parser.parse_args(argv)

    tests = json.loads((arguments.data_dir / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads((arguments.data_dir / "answers.json").read_text("utf-8"))["commits"]
    durations = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
    name_of = {t["number"]: t["id"] for t in tests}
    big_numbers = {t["number"] for t in tests if t["id"] in BIG_TESTS}

    store = MapStore(arguments.maps)
    everything = store.all_tests()
    all_numbers = list(durations)

    rows = []
    total = 0
    oracle_total = 0
    misses: list[tuple[str, str, list[int]]] = []
    fallbacks = 0
    cpu_started = time.process_time()

    for commit_id in sorted(answers):
        patch = (arguments.data_dir / "commits" / f"{commit_id}.patch")
        text = patch.read_text("utf-8", errors="replace")
        fell_back = False
        try:
            bits = choose.select(text, store)
            if bits == everything:
                fell_back = True
                chosen = all_numbers
            else:
                chosen = bits_to_numbers(bits)
        except Exception as error:  # noqa: BLE001
            fell_back = True
            chosen = all_numbers
            if not arguments.quiet:
                print(f"  {commit_id}: fell back, {error!r}", file=sys.stderr)
        fallbacks += fell_back

        failing = answers[commit_id]["failing"]
        chosen_set = set(chosen)
        missed = [n for n in failing if n not in chosen_set]
        cost = cost_of(chosen, durations)
        oracle = cost_of(failing, durations)
        total += cost
        oracle_total += oracle
        if missed:
            misses.append((commit_id, answers[commit_id].get("kind", "?"), missed))
        rows.append({
            "commit": commit_id,
            "kind": answers[commit_id].get("kind", "?"),
            "path": answers[commit_id].get("path", ""),
            "selected": len(chosen),
            "cost": cost,
            "oracle": oracle,
            "missed": missed,
            "fell_back": fell_back,
            "big": sorted(big_numbers & chosen_set),
        })

    cpu = time.process_time() - cpu_started
    simulated = total + MISS_PENALTY * len(misses)

    print(f"commits:        {len(rows)}")
    print(f"simulated:      {simulated / 1000:.3f} s "
          f"({simulated / 1000 / len(rows):.3f} s per commit)")
    print(f"  selection:    {total / 1000:.3f} s")
    print(f"  misses:       {len(misses)} x 300 s = {MISS_PENALTY * len(misses) / 1000:.3f} s")
    print(f"oracle:         {oracle_total / 1000:.3f} s "
          f"({oracle_total / 1000 / len(rows):.3f} s per commit)")
    print(f"over oracle:    {(total - oracle_total) / 1000:.3f} s "
          f"({(total - oracle_total) / 1000 / len(rows):.3f} s per commit)")
    print(f"fell back:      {fallbacks} commits")
    print(f"decision CPU:   {cpu * 1000:.0f} ms total, "
          f"{cpu * 1000 / len(rows):.1f} ms per commit")

    if misses:
        print(f"\nMISSES ({len(misses)}):")
        by_kind = collections.Counter(kind for _, kind, _ in misses)
        for kind, count in by_kind.most_common():
            print(f"  {kind:18s} {count}")
        for commit_id, kind, missed in misses[:30]:
            names = ", ".join(name_of[n] for n in missed[:3])
            print(f"  {commit_id} [{kind}] missed {len(missed)}: {names}")

    by_kind_cost: dict[str, list[int]] = collections.defaultdict(list)
    for row in rows:
        by_kind_cost[row["kind"]].append(row["cost"] - row["oracle"])
    print("\nextra cost over oracle, by kind:")
    for kind in sorted(by_kind_cost, key=lambda k: -sum(by_kind_cost[k])):
        values = by_kind_cost[kind]
        print(f"  {kind:18s} n={len(values):4d}  total={sum(values) / 1000:8.3f} s  "
              f"mean={sum(values) / len(values) / 1000:6.3f} s")

    carrying = [r for r in rows if r["big"]]
    print(f"\ncommits still selecting a 20 s+ test: {len(carrying)}/{len(rows)}")
    for row in carrying[: arguments.show]:
        which = ", ".join(name_of[n].split("::")[-1] for n in row["big"])
        print(f"  {row['commit']:8s} [{row['kind']:14s}] {which}"
              f"{'  (fell back)' if row['fell_back'] else ''}  {row['path']}")

    print(f"\n{arguments.show} costliest commits:")
    for row in sorted(rows, key=lambda r: -r["cost"])[: arguments.show]:
        print(f"  {row['commit']:8s} [{row['kind']:14s}] {row['cost'] / 1000:8.3f} s "
              f"(oracle {row['oracle'] / 1000:7.3f} s) {row['selected']:5d} tests"
              f"{'  FELL BACK' if row['fell_back'] else ''}  {row['path']}")

    return 1 if misses else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
