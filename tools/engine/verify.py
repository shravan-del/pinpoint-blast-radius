"""Prove a test passes by running it, and only then drop it.

The rule this file exists to enforce: a test is removed from the selection
only when we actually ran it, against this commit's code, and saw it pass.
Everything else - our harness raising, a timeout, the budget running out, a
file we cannot reconstruct, a verdict we did not receive - leaves the test in
the selection.  Our machinery failing must never look like a test passing.

The overlay is path-based on purpose; see pinpoint/overlay.py.  A spawned pool
worker is a fresh interpreter and would not inherit an import hook.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from pinpoint.diffparse import ADDED, DELETED, apply_hunks, parse_patch
from tools.engine.overlay import PRISTINE, Overlay

PASS = "pass"
WORKER_COST_MILLISECONDS = 2_000

# What a verified commit costs before any test runs: a fresh interpreter,
# importing sqlglot and the runner, building the overlay and checking it
# landed.  Measured end to end in the scorer's container at ~1.6 s a commit,
# not the 0.42 s that timing `python -c "import sqlglot"` suggests.
PROCESS_START_MILLISECONDS = 1_000

# Running a test only pays when it passes; when it fails we spend the CPU and
# still have to select it.  Across the labeled sets about a third of the tests
# worth running turn out to fail, so the packing saving has to beat our CPU by
# this much before it is worth trying at all.  With no margin, verification
# was a net loss of 11 s on the mutant set.
PROFIT_MARGIN = 2.5


class Budget:
    """Wall clock for the whole batch.  Verification stops before it bites."""

    def __init__(self, seconds: float, reserve: float = 0.40) -> None:
        self.started = time.monotonic()
        self.total = seconds
        self.reserve = reserve

    def remaining(self) -> float:
        spent = time.monotonic() - self.started
        return max(0.0, self.total * (1.0 - self.reserve) - spent)


class Engine:
    """Runs tests against a per-commit overlay of the pinned source."""

    def __init__(self, pristine: Path = PRISTINE) -> None:
        self.pristine = pristine
        self.overlay = Overlay(pristine=pristine)
        self.available = pristine.is_dir()
        self.runs = 0
        self.cpu_milliseconds = 0

    # -- building this commit's tree ---------------------------------------

    def materialise(self, patch_text: str) -> dict[str, str | None] | None:
        """This commit's files, or None when we cannot rebuild them exactly."""
        files: dict[str, str | None] = {}
        for change in parse_patch(patch_text):
            if change.binary:
                return None
            if change.status == DELETED:
                files[change.old_path] = None
                continue
            if change.renamed:
                files[change.old_path] = None
            if change.status == ADDED:
                old_lines: list[str] = []
            else:
                source = self.pristine / (change.old_path or change.path)
                if not source.is_file():
                    return None
                try:
                    old_lines = source.read_text("utf-8").split("\n")
                except (UnicodeDecodeError, OSError):
                    return None
            files[change.path] = "\n".join(
                apply_hunks(old_lines, change.hunks, change.path))
        return files

    # -- running -----------------------------------------------------------

    def run(self, patch_text: str, node_ids: list[str],
            timeout: float) -> dict[str, str]:
        """Verdicts for whichever tests reported one.  Never raises."""
        if not self.available or not node_ids or timeout <= 0:
            return {}
        try:
            files = self.materialise(patch_text)
        except Exception:  # noqa: BLE001
            return {}
        if files is None:
            return {}
        try:
            self.overlay.apply(files)
        except Exception:  # noqa: BLE001
            return {}

        # Prove the overlay really differs from pristine before trusting a
        # pass: an overlay that silently failed to apply would run the pinned
        # code and call every test green.
        try:
            for relative, text in files.items():
                landed = self.overlay.root / relative
                if text is None:
                    if landed.exists():
                        return {}
                elif landed.read_text("utf-8") != text:
                    return {}
        except Exception:  # noqa: BLE001
            return {}

        environment = self.overlay.environment()
        environment["PYTHONPATH"] = f"{self.overlay.root}{os.pathsep}{_app_root()}"
        started = time.monotonic()
        process = None
        verdicts: dict[str, str] = {}
        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "pinpoint.runner"],
                cwd=str(self.overlay.root), env=environment,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True,
                start_new_session=True,      # its own group, so we can kill
                                             # the spawned pool workers too
            )
            out, _ = process.communicate(
                json.dumps({"tests": node_ids}), timeout=timeout)
            for line in (out or "").splitlines():
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if record.get("test") and record.get("status"):
                    verdicts[record["test"]] = record["status"]
        except subprocess.TimeoutExpired:
            _kill_group(process)
            verdicts = {}        # a hang tells us nothing; select them all
        except Exception:  # noqa: BLE001
            _kill_group(process)
            verdicts = {}
        finally:
            if process is not None and process.poll() is None:
                _kill_group(process)
            self.runs += 1
            self.cpu_milliseconds += int((time.monotonic() - started) * 1000)
            try:
                self.overlay.reset()
            except Exception:  # noqa: BLE001
                self.available = False      # stop verifying rather than guess

        # Only a test we asked about and that reported a verdict counts.
        return {k: v for k, v in verdicts.items() if k in set(node_ids)}


def _app_root() -> str:
    return str(Path(__file__).resolve().parent.parent)


def _kill_group(process) -> None:
    if process is None:
        return
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except Exception:  # noqa: BLE001
        try:
            process.kill()
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# Deciding what, if anything, is worth running
# --------------------------------------------------------------------------


def pack_cost(numbers, durations, pack) -> int:
    workers = pack(list(numbers), durations)
    if not workers:
        return 0
    loads = [sum(durations[n] for n in worker) for worker in workers]
    return max(loads) + WORKER_COST_MILLISECONDS * len(loads)


def plan(candidates, durations, costs, pack, budget_milliseconds):
    """Which tests to run, or an empty list when running cannot pay.

    A commit costs the busiest worker plus 2 s a worker, so proving a cheap
    test passes changes nothing while an expensive one is still selected.
    Candidates are therefore considered longest-first and we keep the prefix
    that comes out cheapest, charging our own CPU at the measured rate and
    assuming - pessimistically - that everything we run still has to be
    selected if it fails.
    """
    if not candidates or not costs:
        return []
    blind = pack_cost(candidates, durations, pack)
    order = sorted(candidates, key=lambda n: -durations[n])

    per_test = costs["test"]
    per_file = costs["file"]
    per_class = costs["klass"]
    file_of = costs["file_of"]
    class_of = costs["class_of"]

    best_total, best = blind, []
    spent = PROCESS_START_MILLISECONDS
    paid_files: set[str] = set()
    paid_classes: set[str] = set()
    chosen: list[int] = []
    for number in order:
        cost = per_test.get(number)
        if cost is None:
            break               # unmeasured: stop, do not guess
        path = file_of[number]
        klass = class_of[number]
        extra = cost
        if path not in paid_files:
            extra += per_file.get(path, 0)
        if klass not in paid_classes:
            extra += per_class.get(klass, 0)
        if spent + extra > min(blind, budget_milliseconds):
            break
        spent += extra
        paid_files.add(path)
        paid_classes.add(klass)
        chosen.append(number)
        survivors = [n for n in candidates if n not in set(chosen)]
        saving = blind - pack_cost(survivors, durations, pack)
        if saving > PROFIT_MARGIN * spent and (blind - saving + spent) < best_total:
            best_total = blind - saving + spent
            best = list(chosen)
    return best
