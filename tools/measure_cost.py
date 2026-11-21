#!/usr/bin/env python3
"""What each test costs OUR harness to run, in CPU seconds.

The cost model cannot use tests.json's `seconds`: that is wall time measured
under pytest with a parallel pool, and the board charges us total CPU across
every process we start.  The two differ by 50x for the pool-driven tests.

Measured warm, one process per file, because that is how verification runs
them: pay the module import once, then each test's marginal cost.

Writes maps/costs.json.  Build-time tool; the selector only reads the result.
"""

from __future__ import annotations

import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

PRISTINE = Path(os.environ.get("PINPOINT_PRISTINE", "/app/pristine"))
OUT = Path(os.environ.get("PINPOINT_COSTS", "/work/maps/costs.json"))
TESTS_JSON = Path(os.environ.get("PINPOINT_TESTS_JSON", "/work/sample/tests.json"))
PER_FILE_TIMEOUT = int(os.environ.get("PINPOINT_FILE_TIMEOUT", "400"))

CHILD = r'''
import json, os, resource, sys, time, unittest

def cpu():
    a = resource.getrusage(resource.RUSAGE_SELF)
    b = resource.getrusage(resource.RUSAGE_CHILDREN)
    return a.ru_utime + a.ru_stime + b.ru_utime + b.ru_stime

class Timed(unittest.TestResult):
    """Per-test CPU, and whatever is left over is the class fixture."""
    def __init__(self):
        super().__init__()
        self.per_test = {}
        self.bad = set()
        self._mark = None
    def startTest(self, test):
        super().startTest(test)
        self._mark = cpu()
    def stopTest(self, test):
        super().stopTest(test)
        self.per_test[test.pinpoint_id] = cpu() - self._mark
    def addError(self, test, err):
        super().addError(test, err); self.bad.add(test.pinpoint_id)
    def addFailure(self, test, err):
        super().addFailure(test, err); self.bad.add(test.pinpoint_id)
    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        if err is not None:
            self.bad.add(test.pinpoint_id)

def main():
    request = json.loads(sys.stdin.read())
    sys.path.insert(0, request["root"])
    os.chdir(request["root"])
    c0 = cpu()
    module = __import__(request["module"], fromlist=["*"])
    print(json.dumps({"import_cpu": cpu() - c0}), flush=True)

    by_class = {}
    for node_id in request["tests"]:
        by_class.setdefault(node_id.split("::")[1], []).append(node_id)

    for class_name, node_ids in by_class.items():
        cases = []
        for node_id in node_ids:
            try:
                case = getattr(module, class_name)(node_id.split("::")[2])
            except Exception:
                print(json.dumps({"test": node_id, "cpu": None}), flush=True)
                continue
            case.pinpoint_id = node_id
            cases.append(case)
        if not cases:
            continue
        result = Timed()
        c0 = cpu()
        # One suite for the whole class, so setUpClass runs once - which is
        # how verification runs them.  Running each test in its own suite
        # rebuilt test_optimizer's spawn pool 167 times and said the file
        # cost 529 s.
        unittest.TestSuite(cases).run(result)
        total = cpu() - c0
        for node_id, used in result.per_test.items():
            print(json.dumps({"test": node_id, "cpu": used,
                              "bad": int(node_id in result.bad)}), flush=True)
        print(json.dumps({"class": f"{request['file']}::{class_name}",
                          "setup_cpu": total - sum(result.per_test.values())}),
              flush=True)

if __name__ == "__main__":
    main()
'''


def children_cpu() -> float:
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    return usage.ru_utime + usage.ru_stime


def main() -> int:
    tests = json.loads(TESTS_JSON.read_text("utf-8"))["tests"]
    by_file: dict[str, list[str]] = {}
    for test in tests:
        by_file.setdefault(test["file"], []).append(test["id"])

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(PRISTINE)
    environment["SKIP_INTEGRATION"] = "0"
    for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS"):
        environment[variable] = "1"

    costs: dict[str, float] = {}
    imports: dict[str, float] = {}
    failed_here: list[str] = []
    setups: dict[str, float] = {}
    unmeasured: list[str] = []

    started = time.monotonic()
    for index, (path, node_ids) in enumerate(sorted(by_file.items()), start=1):
        module_name = path[:-3].replace("/", ".")
        request = {"root": str(PRISTINE), "module": module_name,
                   "tests": node_ids, "file": path}
        before = children_cpu()
        try:
            finished = subprocess.run(
                [sys.executable, "-c", CHILD], input=json.dumps(request),
                env=environment, capture_output=True, text=True,
                timeout=PER_FILE_TIMEOUT, start_new_session=True,
            )
            lines = finished.stdout.splitlines()
        except subprocess.TimeoutExpired:
            lines = []
        wall_cpu = children_cpu() - before
        seen = set()
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if "import_cpu" in record:
                imports[path] = round(record["import_cpu"], 4)
            elif "class" in record:
                setups[record["class"]] = round(max(record["setup_cpu"], 0.0), 4)
            elif "test" in record:
                seen.add(record["test"])
                if record["cpu"] is None:
                    unmeasured.append(record["test"])
                else:
                    costs[record["test"]] = round(record["cpu"], 4)
                    if record.get("bad"):
                        failed_here.append(record["test"])
        unmeasured.extend(n for n in node_ids if n not in seen)
        print(f"  [{index:2d}/{len(by_file)}] {path:46s} "
              f"{len(node_ids):4d} tests  harness cpu {wall_cpu:7.2f}s", file=sys.stderr)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "per_test_cpu_ms": {k: int(round(v * 1000)) for k, v in sorted(costs.items())},
        "per_file_import_cpu_ms": {k: int(round(v * 1000)) for k, v in sorted(imports.items())},
        "per_class_setup_cpu_ms": {k: int(round(v * 1000)) for k, v in sorted(setups.items())},
        "failed_in_harness": sorted(set(failed_here)),
        "unmeasured": sorted(set(unmeasured)),
    }, separators=(",", ":")))
    print(f"\nmeasured {len(costs)} tests in {time.monotonic()-started:.0f}s", file=sys.stderr)
    print(f"failed in our harness: {len(set(failed_here))}", file=sys.stderr)
    print(f"unmeasured: {len(set(unmeasured))}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
