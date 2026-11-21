"""Unit tests for the selector.  Run with `python3 -m unittest discover tests`."""

from __future__ import annotations

import itertools
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

import selector  # noqa: E402

sys.path.insert(0, str(REPO))
import score  # noqa: E402


def cost_of(workers: list[list[int]], durations: dict[int, int]) -> int:
    """The scorer's cost for one commit, straight from SCORING.md."""
    occupied = [worker for worker in workers if worker]
    if not occupied:
        return 0
    loads = [sum(durations[number] for number in worker) for worker in occupied]
    return max(loads) + selector.WORKER_COST_MILLISECONDS * len(occupied)


class TestDurations(unittest.TestCase):
    def test_matches_the_scorers_rounding(self):
        for thousandths in range(0, 5000):
            seconds = thousandths / 1000
            self.assertEqual(
                selector.duration_milliseconds(seconds),
                score._duration_milliseconds(seconds),
                seconds,
            )

    def test_halves_round_up(self):
        self.assertEqual(selector.duration_milliseconds(0.0015), 2)
        self.assertEqual(selector.duration_milliseconds(0.0), 0)


class TestPack(unittest.TestCase):
    def test_empty_selection_uses_no_workers(self):
        self.assertEqual(selector.pack([], {}), [])

    def test_single_test_uses_one_worker(self):
        self.assertEqual(selector.pack([7], {7: 500}), [[7]])

    def test_never_drops_or_repeats_a_test(self):
        durations = {number: (number * 37) % 100 + 1 for number in range(60)}
        workers = selector.pack(list(durations), durations)
        flat = [number for worker in workers for number in worker]
        self.assertEqual(sorted(flat), sorted(durations))
        self.assertEqual(len(flat), len(set(flat)), "a test appears twice")

    def test_never_exceeds_eight_workers_and_never_emits_an_empty_one(self):
        for size in (1, 2, 3, 7, 8, 9, 50):
            durations = {number: number + 1 for number in range(size)}
            workers = selector.pack(list(durations), durations)
            self.assertLessEqual(len(workers), selector.MAXIMUM_WORKERS)
            self.assertTrue(all(workers), f"empty worker for size {size}")

    def test_is_deterministic(self):
        durations = {number: (number * 17) % 23 + 1 for number in range(40)}
        first = selector.pack(list(durations), durations)
        second = selector.pack(list(reversed(list(durations))), durations)
        self.assertEqual(first, second)

    def test_beats_or_matches_every_fixed_worker_count(self):
        durations = {number: (number * 29) % 61 + 1 for number in range(37)}
        chosen = cost_of(selector.pack(list(durations), durations), durations)
        for worker_count in range(1, selector.MAXIMUM_WORKERS + 1):
            loads = [0] * worker_count
            for number in sorted(durations, key=lambda n: -durations[n]):
                lightest = loads.index(min(loads))
                loads[lightest] += durations[number]
            occupied = [load for load in loads if load > 0]
            self.assertLessEqual(
                chosen, max(occupied) + selector.WORKER_COST_MILLISECONDS * len(occupied)
            )

    def test_is_optimal_on_small_sets(self):
        """Against exhaustive search, LPT plus the k sweep should be optimal here."""
        for durations in (
            {0: 10, 1: 10, 2: 10},
            {0: 5000, 1: 1, 2: 1, 3: 1},
            {0: 7, 1: 6, 2: 5, 3: 4, 4: 3, 5: 2, 6: 1},
        ):
            numbers = list(durations)
            best = None
            for worker_count in range(1, min(len(numbers), 8) + 1):
                for assignment in itertools.product(range(worker_count), repeat=len(numbers)):
                    buckets: list[list[int]] = [[] for _ in range(worker_count)]
                    for number, bucket in zip(numbers, assignment):
                        buckets[bucket].append(number)
                    cost = cost_of(buckets, durations)
                    best = cost if best is None else min(best, cost)
            self.assertEqual(cost_of(selector.pack(numbers, durations), durations), best)


class TestOutput(unittest.TestCase):
    """End to end: run selector.py over a fake /data and score what it printed."""

    def _run(self, tests: list[dict], patches: dict[str, str]) -> bytes:
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / "tests.json").write_text(json.dumps({"tests": tests, "skipped": []}))
            (data / "commits").mkdir()
            for commit_id, text in patches.items():
                (data / "commits" / f"{commit_id}.patch").write_text(text)
            environment = dict(os.environ, BLAST_RADIUS_DATA=str(data))
            finished = subprocess.run(
                [sys.executable, str(REPO / "selector.py")],
                capture_output=True,
                env=environment,
                check=True,
            )
            return finished.stdout

    def test_one_well_formed_line_per_commit(self):
        tests = [
            {"number": n, "id": f"tests/test_x.py::T::t{n}", "file": "tests/test_x.py",
             "seconds": (n + 1) / 100}
            for n in range(25)
        ]
        patches = {"aaaa": "diff --git a/x b/x\n", "bbbb": "diff --git a/y b/y\n"}
        stdout = self._run(tests, patches)
        answers = {"aaaa": {"failing": [3, 4]}, "bbbb": {"failing": []}}
        result = score.evaluate_run(stdout, tests, answers)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["missed_commits"], [])
        self.assertEqual(len(stdout.decode().splitlines()), 2)

    def test_selects_every_test_so_nothing_can_be_missed(self):
        tests = [
            {"number": n, "id": f"tests/test_x.py::T::t{n}", "file": "tests/test_x.py",
             "seconds": 0.001}
            for n in range(12)
        ]
        stdout = self._run(tests, {"zzzz": "diff --git a/x b/x\n"})
        line = json.loads(stdout.decode().splitlines()[0])
        self.assertEqual(sorted(n for w in line["workers"] for n in w), list(range(12)))

    def test_unreadable_tests_json_does_not_crash(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            (data / "tests.json").write_text("{ this is not json")
            (data / "commits").mkdir()
            (data / "commits" / "abcd.patch").write_text("diff --git a/x b/x\n")
            finished = subprocess.run(
                [sys.executable, str(REPO / "selector.py")],
                capture_output=True,
                env=dict(os.environ, BLAST_RADIUS_DATA=str(data)),
            )
            self.assertEqual(finished.returncode, 0, finished.stderr)
            line = json.loads(finished.stdout.decode().splitlines()[0])
            self.assertEqual(line, {"commit": "abcd", "workers": []})

    def test_a_missing_data_directory_does_not_crash(self):
        finished = subprocess.run(
            [sys.executable, str(REPO / "selector.py")],
            capture_output=True,
            env=dict(os.environ, BLAST_RADIUS_DATA="/nonexistent-blast-radius-data"),
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertEqual(finished.stdout, b"")


class TestAgainstTheRealSampleSet(unittest.TestCase):
    def test_the_packing_is_what_we_expect(self):
        tests = json.loads((REPO / "sample" / "tests.json").read_text())["tests"]
        durations = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
        workers = selector.pack(list(durations), durations)
        self.assertEqual(sorted(n for w in workers for n in w), sorted(durations))
        self.assertLessEqual(len(workers), 8)
        # 1104.510 s over 30 commits, the number in NOTES.md.
        self.assertEqual(cost_of(workers, durations) * 30, 1_104_510)


if __name__ == "__main__":
    unittest.main()
