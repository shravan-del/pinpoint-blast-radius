#!/usr/bin/env python3
"""Where the excess over the oracle actually goes.

Per commit: our cost, the oracle's, the difference, which expensive tests we
selected that never fail, and which rule pulled each one in.  Then the same
totals bucketed, so we can tell how much is "a giant test we cannot rule out"
from how much is "a hundred cheap tests" from "we selected anything at all on
a commit that breaks nothing".

Analysis only; nothing here ships.
"""

from __future__ import annotations

import argparse
import collections
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import selector  # noqa: E402
from pinpoint import astutil, choose, diffparse  # noqa: E402
from pinpoint.mapstore import MapStore, bits_to_numbers  # noqa: E402

WORKER_COST = 2_000
GIANT_MILLISECONDS = 1_000


def cost_of(numbers, durations) -> int:
    workers = selector.pack(list(numbers), durations)
    if not workers:
        return 0
    loads = [sum(durations[n] for n in worker) for worker in workers]
    return max(loads) + WORKER_COST * len(loads)


def workers_used(numbers, durations) -> int:
    return len(selector.pack(list(numbers), durations))


def rule_for_line(path: str, line: int, store, may_narrow: bool) -> str:
    """Which rule decides this changed line."""
    if store.class_at(path, line) is not None:
        return "class-body(narrowed)" if may_narrow else "class-body(guarded)"
    if store.in_def_header(path, line):
        return "def-header(narrowed)" if may_narrow else "def-header(guarded)"
    if not store.has_block(path, line):
        return "module-level"
    return "statement"


def explain_commit(commit_id: str, text: str, store, durations, failing):
    """Per selected giant test, the file and rule that brought it in."""
    everything = store.all_tests()
    reasons: list[str] = []
    giants_by_reason: dict[int, str] = {}
    for change in diffparse.parse_patch(text):
        bits = choose._for_change(change, store, everything)
        path = change.path
        label = f"{path.split('/')[-1]}"
        if change.status != diffparse.MODIFIED or change.binary or not path.endswith(".py"):
            reasons.append(f"{label}:{change.status}{'/binary' if change.binary else ''}")
            for number in bits_to_numbers(bits):
                if durations.get(number, 0) >= GIANT_MILLISECONDS:
                    giants_by_reason.setdefault(number, f"{label}:non-py/{change.status}")
            continue
        old_text = store.source(path)
        if old_text is None:
            reasons.append(f"{label}:no-source")
            continue
        try:
            new_lines = diffparse.apply_hunks(old_text.split("\n"), change.hunks, path)
        except diffparse.PatchError:
            reasons.append(f"{label}:unapplyable")
            continue
        import ast as _ast
        try:
            may_narrow = store.has_class_map and (
                astutil.names_and_strings(_ast.parse(old_text))
                == astutil.names_and_strings(_ast.parse("\n".join(new_lines)))
            )
        except SyntaxError:
            may_narrow = False
        seen_rules = set()
        for hunk in change.hunks:
            changed, insertions = diffparse.touched_old_lines(hunk)
            for line in changed:
                rule = rule_for_line(path, line, store, may_narrow)
                seen_rules.add(rule)
                if rule.startswith("class-body") and may_narrow:
                    got = choose._class_body_tests(path, line, store) or 0
                elif rule.startswith("def-header") and may_narrow:
                    got = store.tests_for_block(path, line)
                else:
                    got = store.tests_for_line(path, line)
                for number in bits_to_numbers(got):
                    if durations.get(number, 0) >= GIANT_MILLISECONDS:
                        giants_by_reason.setdefault(number, f"{label}:{rule}")
            for line in insertions:
                seen_rules.add("insertion")
                got = store.tests_for_line(path, line)
                got |= (store.tests_for_block(path, line) if store.has_block(path, line)
                        else store.tests_in_module(path))
                for number in bits_to_numbers(got):
                    if durations.get(number, 0) >= GIANT_MILLISECONDS:
                        giants_by_reason.setdefault(number, f"{label}:insertion")
        reasons.append(f"{label}:{'+'.join(sorted(seen_rules)) or 'no-hunks'}")
    return reasons, giants_by_reason


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data_dir", type=Path)
    parser.add_argument("--maps", type=Path, default=REPO / "maps" / "maps.zip")
    parser.add_argument("--top", type=int, default=20)
    arguments = parser.parse_args(argv)

    tests = json.loads((arguments.data_dir / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads((arguments.data_dir / "answers.json").read_text("utf-8"))["commits"]
    durations = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
    name_of = {t["number"]: t["id"] for t in tests}
    store = MapStore(arguments.maps)
    everything = store.all_tests()

    rows = []
    bucket_giant = bucket_small = bucket_workers = 0
    nothing_fails_cost = 0
    nothing_fails_count = 0
    giant_blame: dict[str, int] = collections.Counter()

    for commit_id in sorted(answers):
        text = (arguments.data_dir / "commits" / f"{commit_id}.patch").read_text(
            "utf-8", errors="replace")
        try:
            bits = choose.select(text, store)
            chosen = (list(durations) if bits == everything else bits_to_numbers(bits))
        except Exception:  # noqa: BLE001
            chosen = list(durations)
        failing = answers[commit_id]["failing"]
        ours = cost_of(chosen, durations)
        oracle = cost_of(failing, durations)

        extra = [n for n in chosen if n not in set(failing)]
        extra_giants = [n for n in extra if durations[n] >= GIANT_MILLISECONDS]
        with_giants = cost_of(list(failing) + extra_giants, durations)
        bucket_giant += with_giants - oracle
        bucket_small += ours - with_giants
        bucket_workers += WORKER_COST * (
            workers_used(chosen, durations) - workers_used(failing, durations))
        if not failing:
            nothing_fails_cost += ours
            nothing_fails_count += 1

        reasons, giants_by_reason = explain_commit(
            commit_id, text, store, durations, failing)
        for number in extra_giants:
            giant_blame[giants_by_reason.get(number, "?")] += durations[number]

        rows.append({
            "commit": commit_id,
            "kind": answers[commit_id].get("kind", "?"),
            "ours": ours, "oracle": oracle, "excess": ours - oracle,
            "selected": len(chosen), "failing": len(failing),
            "giants": sorted(
                (name_of[n].split("::")[-1], durations[n],
                 giants_by_reason.get(n, "?"))
                for n in extra_giants),
            "reasons": "; ".join(reasons),
        })

    rows.sort(key=lambda r: -r["excess"])
    total_excess = sum(r["excess"] for r in rows)
    total_ours = sum(r["ours"] for r in rows)
    total_oracle = sum(r["oracle"] for r in rows)

    print(f"{'cid':8s} {'kind':16s} {'ours':>8s} {'oracle':>8s} {'excess':>8s} "
          f"{'sel':>5s} {'fail':>4s}  giants we did not need / reason")
    for row in rows[: arguments.top]:
        giants = ", ".join(f"{n}({s / 1000:.1f}s via {why})" for n, s, why in row["giants"])
        print(f"{row['commit']:8s} {row['kind']:16s} {row['ours'] / 1000:8.3f} "
              f"{row['oracle'] / 1000:8.3f} {row['excess'] / 1000:8.3f} "
              f"{row['selected']:5d} {row['failing']:4d}  {giants or '-'}")
        print(f"{'':8s} rules: {row['reasons'][:150]}")

    n = len(rows)
    print(f"\ncommits {n}   ours {total_ours / 1000:.3f}s ({total_ours / 1000 / n:.3f}/commit)"
          f"   oracle {total_oracle / 1000:.3f}s ({total_oracle / 1000 / n:.3f}/commit)"
          f"   excess {total_excess / 1000:.3f}s ({total_excess / 1000 / n:.3f}/commit)")
    print("\nEXCESS BY BUCKET")
    print(f"  (a) giant tests selected but not failing   {bucket_giant / 1000:9.3f} s "
          f"({100 * bucket_giant / max(total_excess, 1):5.1f}%)")
    print(f"  (b) small tests selected but not failing   {bucket_small / 1000:9.3f} s "
          f"({100 * bucket_small / max(total_excess, 1):5.1f}%)")
    print(f"  (c) commits where nothing fails at all     {nothing_fails_cost / 1000:9.3f} s "
          f"over {nothing_fails_count} commits  [cuts across a and b]")
    print(f"  (d) worker overhead, 2 s each              {bucket_workers / 1000:9.3f} s "
          f"[part of a and b]")

    print("\nWHICH RULE PULLS IN THE GIANT TESTS (seconds of giant test, by rule)")
    for why, milliseconds in giant_blame.most_common(14):
        print(f"  {milliseconds / 1000:9.3f} s  {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
