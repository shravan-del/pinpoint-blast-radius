#!/usr/bin/env python3
"""Upper bound on what running tests can win, using the measured CPU costs.

Assumes the engine always reports the truth and never times out, i.e. it is
generous to verification.  For each commit it tries every prefix of the
candidates ordered longest-first - the only tests worth running are the ones
binding the busiest worker - and also running the lot, and takes the cheapest.

Simulation only.  Nothing here ships.
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
PROCESS_START_MS = 420
PREFIX_CAP = 120


def pack_cost(numbers, durations):
    workers = selector.pack(list(numbers), durations)
    if not workers:
        return 0
    return max(sum(durations[n] for n in w) for w in workers) + WORKER * len(workers)


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--maps", type=Path, default=REPO / "maps" / "maps.zip")
    parser.add_argument("--costs", type=Path, default=REPO / "maps" / "costs.json")
    parser.add_argument("--free-giants", action="store_true",
                        help="pretend every giant can be proved by parts for 1 s")
    options = parser.parse_args(argv)

    tests = json.loads((options.data_dir / "tests.json").read_text())["tests"]
    answers = json.loads((options.data_dir / "answers.json").read_text())["commits"]
    durations = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
    number_of = {t["id"]: t["number"] for t in tests}
    file_of = {t["number"]: t["file"] for t in tests}
    class_of = {t["number"]: f"{t['file']}::{t['id'].split('::')[1]}" for t in tests}

    costs = json.loads(options.costs.read_text())
    cpu_ms = {number_of[k]: v for k, v in costs["per_test_cpu_ms"].items()
              if k in number_of}
    import_ms = costs["per_file_import_cpu_ms"]
    class_ms = costs["per_class_setup_cpu_ms"]

    store = MapStore(options.maps)
    everything = store.all_tests()

    blind_total = best_total = cpu_total = 0
    changed = 0
    unverifiable_binding = {}
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

        order = sorted(candidates, key=lambda n: -durations[n])
        best, best_cpu = blind, 0
        spent = PROCESS_START_MS
        paid_files, paid_classes = set(), set()
        for index, number in enumerate(order[:PREFIX_CAP], start=1):
            cost = cpu_ms.get(number)
            if cost is None:
                break           # cannot run it, so cannot run anything past it
            if options.free_giants and durations[number] >= 1000:
                cost = min(cost, 1000)
            spent += cost
            if file_of[number] not in paid_files:
                spent += import_ms.get(file_of[number], 0)
                paid_files.add(file_of[number])
            if class_of[number] not in paid_classes:
                spent += class_ms.get(class_of[number], 0)
                paid_classes.add(class_of[number])
            verified = set(order[:index])
            survivors = [n for n in candidates if n not in verified or n in failing]
            total = spent + pack_cost(survivors, durations)
            if total < best:
                best, best_cpu = total, spent
            if spent > blind:
                break
        best_total += best
        cpu_total += best_cpu
        if best < blind:
            changed += 1
        else:
            top = order[0]
            unverifiable_binding[top] = unverifiable_binding.get(top, 0) + 1

    n = len(answers)
    tag = " (giants free)" if options.free_giants else ""
    print(f"{options.data_dir}{tag}")
    print(f"  blind            {blind_total/1000:9.3f} s ({blind_total/1000/n:.3f}/commit)")
    print(f"  best with runs   {best_total/1000:9.3f} s ({best_total/1000/n:.3f}/commit)"
          f"   improved {changed}/{n}")
    print(f"  CPU spent        {cpu_total/1000:9.3f} s")
    print(f"  saved            {(blind_total-best_total)/1000:9.3f} s")
    if unverifiable_binding:
        name = {t["number"]: t["id"].split("::")[-1] for t in tests}
        worst = sorted(unverifiable_binding.items(), key=lambda kv: -kv[1])[:5]
        print("  commits not improved, their longest candidate: "
              + ", ".join(f"{name[k]}x{v}" for k, v in worst))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
