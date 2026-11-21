"""Run chosen tests against the overlay and report pass/fail, one line each.

Started as a child process with the overlay directory as cwd and on
PYTHONPATH.  It must never be imported by the selector: the point is that it
runs in its own interpreter, against the patched tree.

Results are written as they happen, one JSON object per line, flushed.  If the
parent has to kill us - a patch that loops forever, a budget running out - the
verdicts that already arrived are still good, and everything else is simply
unknown and gets selected blindly.

The `if __name__ == "__main__"` guard at the bottom is load-bearing.
tests/test_executor.py starts worker interpreters with the spawn method, and
spawn re-imports the main module in each one.  Without the guard every worker
re-runs this whole file.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
import unittest

PASS = "pass"
FAIL = "fail"


class _Outcome(unittest.TestResult):
    """unittest's result, reduced to the scorer's notion of failure.

    The scorer counts a failure, an error, a timeout, or a file that will not
    import.  A test with subtests is one test, and one failing subtest fails
    it - which is what `addSubTest` with a non-None err means.  An expected
    failure and a skip are not failures.
    """

    def __init__(self) -> None:
        super().__init__()
        self.failed: set[str] = set()
        self.ran: set[str] = set()

    @staticmethod
    def _name(test) -> str:
        return getattr(test, "pinpoint_id", None) or test.id()

    def startTest(self, test) -> None:
        super().startTest(test)
        self.ran.add(self._name(test))

    def addError(self, test, err) -> None:
        super().addError(test, err)
        self.failed.add(self._name(test))

    def addFailure(self, test, err) -> None:
        super().addFailure(test, err)
        self.failed.add(self._name(test))

    def addSubTest(self, test, subtest, err) -> None:
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.failed.add(self._name(test))


def _emit(record: dict) -> None:
    sys.stdout.write(json.dumps(record, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _load(node_id: str):
    """Build the one TestCase a node id names, or raise."""
    path, class_name, method = node_id.split("::")
    module_name = path[:-3].replace("/", ".")
    module = __import__(module_name, fromlist=["*"])
    case = getattr(module, class_name)(method)
    case.pinpoint_id = node_id
    return case


def run(node_ids: list[str]) -> None:
    # Group by class so setUpClass runs once per class, as it does under
    # pytest, and keep the given order inside a class.
    by_class: dict[tuple[str, str], list[str]] = {}
    for node_id in node_ids:
        path, class_name, _ = node_id.split("::")
        by_class.setdefault((path, class_name), []).append(node_id)

    for (path, class_name), members in by_class.items():
        try:
            cases = [_load(node_id) for node_id in members]
        except Exception:  # noqa: BLE001
            # The file will not import: under the scorer every test in it
            # fails, so say so rather than staying silent.
            for node_id in members:
                _emit({"test": node_id, "status": FAIL, "why": "import"})
            continue

        suite = unittest.TestSuite(cases)
        outcome = _Outcome()
        try:
            suite.run(outcome)
        except Exception:  # noqa: BLE001
            _emit({"error": "suite raised", "detail": traceback.format_exc()[-400:]})
            return
        for node_id in members:
            if node_id not in outcome.ran:
                # Never started: a class fixture blew up, or a skip we cannot
                # tell apart.  Unknown, so the caller selects it.
                continue
            _emit({"test": node_id,
                   "status": FAIL if node_id in outcome.failed else PASS})


def main() -> int:
    request = json.loads(sys.stdin.read() or "{}")
    node_ids = request.get("tests") or []
    sys.path.insert(0, os.getcwd())
    try:
        run(node_ids)
    except Exception:  # noqa: BLE001
        _emit({"error": "runner raised", "detail": traceback.format_exc()[-400:]})
        return 1
    _emit({"done": True})
    return 0


if __name__ == "__main__":
    sys.exit(main())
