#!/usr/bin/env python3
"""Does our harness agree with the scorer about every test it would remove?

For every labeled commit, take the plan the cost model would run, run it
through the engine, and compare each verdict against the ground truth.  A
"pass" where the truth is "fails" is the one outcome that costs 300 s, so the
agreement has to be total before verification is allowed to remove anything.

Runs inside the submission image, which has the pinned source at /app/pristine.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, "/app")
import selector  # noqa: E402
from pinpoint import choose, verify  # noqa: E402
from pinpoint.mapstore import MapStore, bits_to_numbers  # noqa: E402


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("data_dirs", nargs="+", type=Path)
    parser.add_argument("--maps", type=Path, default=Path("/app/maps/maps.zip"))
    parser.add_argument("--costs", type=Path, default=Path("/app/maps/costs.json"))
    options = parser.parse_args(argv)

    store = MapStore(options.maps)
    everything = store.all_tests()
    engine = verify.Engine()
    print(f"engine available: {engine.available}")

    agree = disagree = 0
    planned_commits = 0
    totals = {}
    worst: list[str] = []
    matrix = {("pass", "passes"): 0, ("pass", "fails"): 0,
              ("fail", "passes"): 0, ("fail", "fails"): 0}

    for data_dir in options.data_dirs:
        tests = json.loads((data_dir / "tests.json").read_text())["tests"]
        answers = json.loads((data_dir / "answers.json").read_text())["commits"]
        durations = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
        costs = selector._load_costs(options.costs, tests)
        blind_ms = verified_ms = 0
        cpu_before = engine.cpu_milliseconds
        for commit_id in sorted(answers):
            text = (data_dir / "commits" / f"{commit_id}.patch").read_text(
                "utf-8", errors="replace")
            try:
                bits = choose.select(text, store)
                candidates = (list(durations) if bits == everything
                              else bits_to_numbers(bits))
            except Exception:  # noqa: BLE001
                candidates = list(durations)
            blind = verify.pack_cost(candidates, durations, selector.pack)
            blind_ms += blind
            wanted = (verify.plan(candidates, durations, costs, selector.pack, 25_000)
                      if bits != everything else [])
            if not wanted:
                verified_ms += blind
                continue
            planned_commits += 1
            node_ids = [costs["id_of"][n] for n in wanted]
            verdicts = engine.run(text, node_ids, timeout=120)
            failing = set(answers[commit_id]["failing"])
            proved = {n for n, nid in zip(wanted, node_ids)
                      if verdicts.get(nid) == verify.PASS}
            survivors = [n for n in candidates if n not in proved]
            verified_ms += verify.pack_cost(survivors, durations, selector.pack)
            for number, node_id in zip(wanted, node_ids):
                got = verdicts.get(node_id)
                if got is None:
                    continue
                truth = "fails" if number in failing else "passes"
                matrix[(got, truth)] += 1
                if (got == "pass") == (truth == "passes"):
                    agree += 1
                else:
                    disagree += 1
                    worst.append(f"{data_dir.name}/{commit_id} {node_id}: "
                                 f"we said {got}, truth {truth}")

        totals[data_dir.name] = (
            blind_ms, verified_ms, engine.cpu_milliseconds - cpu_before, len(answers))

    print("\nSCORE EFFECT (simulated seconds, plus the CPU we spend)")
    for name, (blind_ms, verified_ms, cpu_ms, n) in totals.items():
        net = (blind_ms - verified_ms - cpu_ms) / 1000
        print(f"  {name:12s} blind {blind_ms/1000:9.3f} ({blind_ms/1000/n:5.3f}/c)"
              f"  verified {verified_ms/1000:9.3f} ({verified_ms/1000/n:5.3f}/c)"
              f"  our CPU {cpu_ms/1000:7.3f}  NET {net:+8.3f} s"
              f" ({net/n:+6.3f}/c)")

    print(f"\ncommits with a plan: {planned_commits}")
    print(f"verdicts compared:   {agree + disagree}   agree {agree}  disagree {disagree}")
    print("\n  our verdict / the truth")
    for (got, truth), count in sorted(matrix.items()):
        flag = "   <-- A MISS" if got == "pass" and truth == "fails" else ""
        print(f"    {got:5s} / {truth:7s}  {count}{flag}")
    for line in worst[:20]:
        print("  " + line)
    return 1 if disagree else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
