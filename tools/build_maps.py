#!/usr/bin/env python3
"""Build the static maps the selector ships with.

Run inside the lab image (``tools/lab/Dockerfile``), which is the scorer's
Python 3.13 with ``requirements.lock`` and sqlglot at the pinned commit on
``PYTHONPATH``.  Nothing here runs at scoring time: the outputs are committed.

Every test is run **alone, in a fresh interpreter**, under coverage.  That is
much slower than one run with dynamic contexts, and it is the only way to get
the answer right: in a shared run ``setUpClass`` lines are credited only to the
first test of a class, ``lru_cache`` and sqlglot's dialect caches hide lines
from every test after the first to reach them, and anything executed while
pytest collects belongs to no test at all.

Subcommands:
  run     run each test alone and record coverage, imports and file reads
  build   turn the recordings into the compact maps the selector loads
  check   the sanity report
"""

from __future__ import annotations

import argparse
import ast
import gzip
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pinpoint import astutil  # noqa: E402

SQLGLOT_ROOT = Path(os.environ.get("PINPOINT_SQLGLOT_ROOT", "/src/sqlglot"))
REPO = Path(os.environ.get("PINPOINT_REPO", "/work"))
WORK = Path(os.environ.get("PINPOINT_WORK", "/work/eval/maps-work"))
OUT = Path(os.environ.get("PINPOINT_OUT", "/work/maps"))
TESTS_JSON = Path(os.environ.get("PINPOINT_TESTS_JSON", "/work/sample/tests.json"))

PINNED_COMMIT = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"

COVERAGE_RC = """\
[run]
branch = False
parallel = True
patch = _exit, fork
source =
    {root}/sqlglot
    {root}/tests
"""


# --------------------------------------------------------------------------
# run: one fresh process per test
# --------------------------------------------------------------------------


def _read_tests() -> list[dict]:
    return json.loads(TESTS_JSON.read_text("utf-8"))["tests"]


def _run_one(job: tuple) -> dict:
    """Run one test alone and collect everything it touched.

    With ``mode="coverage"`` the test runs under coverage and we keep the line
    map.  With ``mode="reads"`` it runs plain, which is several times faster and
    keeps coverage's own file handling out of the read map; that pass is the one
    whose data-read and directory-scan records we trust.
    """
    import coverage

    test, mode = job
    number = test["number"]
    node_id = test["id"]
    directory = WORK / ("raw" if mode == "coverage" else "raw-reads") / str(number)
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)

    rcfile = directory / "coveragerc"
    rcfile.write_text(COVERAGE_RC.format(root=SQLGLOT_ROOT))

    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPO / "tools" / "maps"), str(SQLGLOT_ROOT)]
    )
    if mode == "coverage":
        environment["COVERAGE_PROCESS_START"] = str(rcfile)
        environment["COVERAGE_FILE"] = str(directory / ".coverage")
    environment["PINPOINT_AUDIT_DIR"] = str(directory)
    environment["PINPOINT_REPO_ROOT"] = str(SQLGLOT_ROOT)
    environment["SKIP_INTEGRATION"] = "0"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    prefix = (
        [sys.executable, "-m", "coverage", "run", f"--rcfile={rcfile}"]
        if mode == "coverage"
        else [sys.executable]
    )
    started = time.monotonic()
    try:
        finished = subprocess.run(
            [
                *prefix,
                "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
                "--timeout", "900", node_id,
            ],
            cwd=str(SQLGLOT_ROOT),
            env=environment,
            capture_output=True,
            text=True,
            timeout=1800,
        )
        exit_code = finished.returncode
        tail = (finished.stdout or "")[-2000:] + (finished.stderr or "")[-2000:]
    except subprocess.TimeoutExpired:
        exit_code = -1
        tail = "TIMEOUT"
    elapsed = time.monotonic() - started

    # Merge every coverage data file the test and its children wrote.
    lines: dict[str, list[int]] = {}
    root_prefix = str(SQLGLOT_ROOT).rstrip("/") + "/"
    for data_path in sorted(directory.glob(".coverage*")):
        try:
            data = coverage.CoverageData(basename=str(data_path))
            data.read()
        except Exception:  # noqa: BLE001 - a corrupt child file must not lose the test
            continue
        for measured in data.measured_files():
            if not measured.startswith(root_prefix):
                continue
            relative = measured[len(root_prefix):]
            found = data.lines(measured) or []
            if found:
                lines.setdefault(relative, []).extend(found)
    lines = {path: sorted(set(numbers)) for path, numbers in sorted(lines.items())}

    reads: set[str] = set()       # opened as data
    import_reads: set[str] = set()  # opened by the import machinery
    imports: set[str] = set()
    scans: set[str] = set()
    for record in sorted(directory.glob("seen-*.txt")):
        for line in record.read_text("utf-8", errors="replace").splitlines():
            if line.startswith("O "):
                reads.add(line[2:])
            elif line.startswith("M "):
                import_reads.add(line[2:])
            elif line.startswith("I "):
                imports.add(line[2:])
            elif line.startswith("D "):
                scans.add(line[2:])

    result = {
        "number": number,
        "id": node_id,
        "exit_code": exit_code,
        "seconds": round(elapsed, 3),
        "lines": lines,
        "reads": sorted(reads),
        "import_reads": sorted(import_reads),
        "imports": sorted(imports),
        "scans": sorted(scans),
        "tail": "" if exit_code == 0 else tail,
    }

    target = WORK / ("tests" if mode == "coverage" else "reads") / f"{number}.json.gz"
    target.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(target, "wt", encoding="utf-8") as handle:
        json.dump(result, handle)
    shutil.rmtree(directory, ignore_errors=True)

    return {
        "number": number,
        "id": node_id,
        "exit_code": exit_code,
        "seconds": result["seconds"],
        "files": len(lines),
        "covered": sum(len(v) for v in lines.values()),
        "reads": len(reads),
        "imports": len(imports),
        "scans": len(scans),
        "tail": result["tail"],
    }


def command_run(arguments: argparse.Namespace) -> int:
    mode = arguments.mode
    out_name = "tests" if mode == "coverage" else "reads"
    tests = _read_tests()
    if arguments.only:
        wanted = set(arguments.only)
        tests = [t for t in tests if str(t["number"]) in wanted or t["id"] in wanted]
    if arguments.resume:
        tests = [t for t in tests
                 if not (WORK / out_name / f"{t['number']}.json.gz").exists()]

    (WORK / out_name).mkdir(parents=True, exist_ok=True)
    print(f"running {len(tests)} tests in {mode} mode, {arguments.jobs} at a time",
          file=sys.stderr)

    failures = []
    done = 0
    started = time.monotonic()
    with ProcessPoolExecutor(max_workers=arguments.jobs) as pool:
        futures = {pool.submit(_run_one, (test, mode)): test for test in tests}
        for future in as_completed(futures):
            summary = future.result()
            done += 1
            if summary["exit_code"] != 0:
                failures.append(summary)
                print(f"  FAILED #{summary['number']} {summary['id']} "
                      f"exit={summary['exit_code']}", file=sys.stderr)
            if done % 50 == 0 or done == len(tests):
                rate = done / max(time.monotonic() - started, 1e-9)
                print(f"  {done}/{len(tests)}  {rate:.1f} tests/s", file=sys.stderr)

    print(f"done in {time.monotonic() - started:.0f}s, {len(failures)} failed",
          file=sys.stderr)
    if failures:
        (WORK / "failures.json").write_text(json.dumps(failures, indent=2))
    return 0



# --------------------------------------------------------------------------
# build: turn the recordings into the maps the selector loads
# --------------------------------------------------------------------------

IGNORED_READ_PREFIXES = ("__pycache__/",)
IGNORED_READ_SUFFIXES = (".pyc", ".pyo")


def _is_interesting_read(path: str) -> bool:
    if path.endswith(IGNORED_READ_SUFFIXES):
        return False
    return not any(part.startswith("__pycache__") for part in path.split("/"))


def _module_name(relative_path: str) -> str | None:
    """The dotted module a repo-relative .py path is imported as."""
    if not relative_path.endswith(".py"):
        return None
    parts = relative_path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


class _Bitsets:
    """A table of test sets, deduplicated, addressed by index."""

    def __init__(self, test_count: int) -> None:
        self.width = (test_count + 7) // 8
        self.table: list[bytes] = []
        self.by_value: dict[bytes, int] = {}

    def add(self, numbers) -> int:
        raw = bytearray(self.width)
        for number in numbers:
            raw[number >> 3] |= 1 << (number & 7)
        value = bytes(raw)
        found = self.by_value.get(value)
        if found is None:
            found = len(self.table)
            self.by_value[value] = found
            self.table.append(value)
        return found


def _runs(per_line: dict[int, int], line_count: int) -> list[list[int]]:
    """Run-length encode line -> value as [[first_line, value], ...]."""
    encoded: list[list[int]] = []
    previous = None
    for line in range(1, line_count + 2):
        value = per_line.get(line, -1)
        if value != previous:
            encoded.append([line, value])
            previous = value
    return encoded


def command_build(arguments: argparse.Namespace) -> int:
    tests = _read_tests()
    test_count = len(tests)
    tests_in_file: dict[str, list[int]] = {}
    for test in tests:
        tests_in_file.setdefault(test["file"], []).append(test["number"])

    # ---- load every recording -------------------------------------------
    covered: dict[str, dict[int, set[int]]] = {}   # path -> line -> tests
    openers: dict[str, set[int]] = {}              # path -> tests that read it as data
    importers: dict[str, set[int]] = {}            # module -> tests
    module_tests: dict[str, set[int]] = {}         # path -> tests that ran any line
    scanners: dict[str, set[int]] = {}             # directory -> tests that listed it
    missing: list[int] = []
    failed: list[dict] = []

    for test in tests:
        number = test["number"]
        record_path = WORK / "tests" / f"{number}.json.gz"
        if not record_path.exists():
            missing.append(number)
            continue
        with gzip.open(record_path, "rt", encoding="utf-8") as handle:
            record = json.load(handle)
        if record["exit_code"] != 0:
            failed.append({"number": number, "id": record["id"],
                           "exit_code": record["exit_code"],
                           "tail": record["tail"][-600:]})
        for path, lines in record["lines"].items():
            per_line = covered.setdefault(path, {})
            for line in lines:
                per_line.setdefault(line, set()).add(number)
            module_tests.setdefault(path, set()).add(number)
        for module in record["imports"]:
            importers.setdefault(module, set()).add(number)

        # Reads and directory scans come from the no-coverage pass when we have
        # one, so coverage's own file handling cannot leak into them.
        reads_path = WORK / "reads" / f"{number}.json.gz"
        if reads_path.exists():
            with gzip.open(reads_path, "rt", encoding="utf-8") as handle:
                reads_record = json.load(handle)
        else:
            reads_record = record
        for path in reads_record.get("reads", ()):
            if _is_interesting_read(path):
                openers.setdefault(path, set()).add(number)
        for module in reads_record.get("imports", ()):
            importers.setdefault(module, set()).add(number)
        for directory in reads_record.get("scans", ()):
            scanners.setdefault(directory.rstrip("/"), set()).add(number)

    if missing:
        print(f"WARNING: {len(missing)} tests have no recording: {missing[:20]}",
              file=sys.stderr)

    # ---- per-file maps ---------------------------------------------------
    bitsets = _Bitsets(test_count)
    entries: dict[str, dict] = {}
    sources: dict[str, bytes] = {}
    own_line_hits = 0
    statement_fallbacks = 0
    dead_lines = 0

    source_paths = sorted(
        str(path.relative_to(SQLGLOT_ROOT))
        for pattern in ("sqlglot/**/*.py", "tests/**/*.py")
        for path in SQLGLOT_ROOT.glob(pattern)
    )
    # Anything a test read or imported counts too, even if it is not a .py file.
    all_paths = sorted(set(source_paths) | set(openers) | set(covered))

    for relative in all_paths:
        absolute = SQLGLOT_ROOT / relative
        entry: dict = {}
        module = _module_name(relative)

        per_line_tests = covered.get(relative, {})
        if relative.endswith(".py") and absolute.is_file():
            raw = absolute.read_bytes()
            sources[relative] = raw
            text = raw.decode("utf-8")
            line_count = len(text.splitlines())
            entry["sha256"] = hashlib.sha256(raw).hexdigest()
            entry["lines"] = line_count
            entry["ast"] = astutil.fingerprint(text)

            try:
                tree = ast.parse(text)
            except SyntaxError:
                tree = None
            if tree is not None:
                owners = astutil.statement_owners(tree)
                blocks = astutil.block_ranges(tree)

                # A line maps to every test that ran ANY part of the statement
                # it belongs to, never to the tests that ran that one line.
                #
                # CPython does give the parts of a multi-line statement their
                # own line numbers, so the finer map exists and is tempting.
                # It is wrong.  Changing one line of a statement can change
                # which of its other lines run at all: the rszg sample turns
                # `and` into `or` on the third line of a four-line condition,
                # and a test that short-circuited before that line now reaches
                # it and fails.  Line coverage says that test never ran the
                # line; the truth is that the change is what makes it run.
                #
                # Running some part of a statement is the only way a change to
                # another part of it can reach you, so the union over the
                # statement is the tightest safe unit.
                owned: dict[int, list[int]] = {}
                for line, anchor in owners.items():
                    owned.setdefault(anchor, []).append(line)

                union_cache: dict[int, set[int]] = {}
                group_of_line = {}
                for line in range(1, line_count + 1):
                    anchor = owners.get(line)
                    runners: set[int] | None = None
                    if anchor is not None:
                        runners = union_cache.get(anchor)
                        if runners is None:
                            runners = set()
                            for sibling in owned.get(anchor, ()):
                                runners |= per_line_tests.get(sibling, set())
                            union_cache[anchor] = runners
                    if runners:
                        if per_line_tests.get(line):
                            own_line_hits += 1
                        else:
                            statement_fallbacks += 1
                    else:
                        dead_lines += 1
                    group_of_line[line] = bitsets.add(runners) if runners else -1
                entry["runs"] = _runs(group_of_line, line_count)

                block_group_cache: dict[tuple[int, int], int] = {}
                group_of_block_line = {}
                for line, span in blocks.items():
                    found = block_group_cache.get(span)
                    if found is None:
                        inside: set[int] = set()
                        for block_line in range(span[0], span[1] + 1):
                            inside |= per_line_tests.get(block_line, set())
                        found = bitsets.add(inside) if inside else -1
                        block_group_cache[span] = found
                    group_of_block_line[line] = found
                entry["block_runs"] = _runs(group_of_block_line, line_count)

        ran = module_tests.get(relative)
        entry["module"] = bitsets.add(ran) if ran else -1
        opened = openers.get(relative)
        entry["openers"] = bitsets.add(opened) if opened else -1
        imported = importers.get(module) if module else None
        entry["importers"] = bitsets.add(imported) if imported else -1
        in_file = tests_in_file.get(relative)
        entry["own_tests"] = bitsets.add(in_file) if in_file else -1

        entries[relative] = entry

    known_modules = {
        _module_name(relative) for relative in all_paths if relative.endswith(".py")
    }

    # ---- package __init__ map, for reasoning about new files -------------
    packages: dict[str, int] = {}
    for relative in all_paths:
        if relative.endswith("/__init__.py"):
            package = relative[: -len("/__init__.py")]
            ran = module_tests.get(relative)
            packages[package] = bitsets.add(ran) if ran else -1

    # Directories a test enumerated *itself*, with the import machinery's own
    # listings filtered out.  A file that did not exist at the pinned commit
    # can only be noticed by a test that looks at its directory, and this is
    # the measurement of exactly that: tests/test_typing.py walking
    # sqlglot/typing/ with pkgutil.iter_modules shows up here as one test.
    #
    # The parent directories of files read as data are deliberately NOT folded
    # in.  Every test reads pyproject.toml, so the repo root would then watch
    # everything and every new file anywhere would select all 1620 tests.
    watchers: dict[str, set[int]] = {}
    for directory, numbers in scanners.items():
        # test_sqlglotc reaches sqlglot/ as sqlglotc/../sqlglot/...
        normalised = os.path.normpath(directory) if directory else ""
        watchers.setdefault(normalised, set()).update(numbers)
    directory_watchers = {
        directory: bitsets.add(numbers) for directory, numbers in watchers.items()
    }

    # meta.json is read on every commit, so it stays small.  The long lists go
    # in their own entries and are only read when a rule actually needs them.
    # Modules that tests tried to import but which have no file at the pinned
    # commit.  The "import" audit event fires even when the import then fails,
    # so sqlglot/__init__.py's `from sqlglot._version import __version__`
    # inside a try/except is recorded here.  Adding such a file later is not an
    # inert new file: it makes that import start succeeding.
    absent_modules = {
        module: bitsets.add(numbers)
        for module, numbers in sorted(importers.items())
        if module not in known_modules
    }

    meta = {
        "pinned_commit": PINNED_COMMIT,
        "test_count": test_count,
        "bitset_width": bitsets.width,
        "packages": packages,
        "directory_watchers": directory_watchers,
        "absent_modules": absent_modules,
    }
    repo_paths = sorted(
        str(path.relative_to(SQLGLOT_ROOT))
        for path in SQLGLOT_ROOT.rglob("*")
        if path.is_file() and not str(path.relative_to(SQLGLOT_ROOT)).startswith(".git/")
        and "__pycache__" not in str(path)
    )
    report = {
        "pinned_commit": PINNED_COMMIT,
        "test_count": test_count,
        "files_mapped": len(entries),
        "sources_bundled": len(sources),
        "distinct_test_sets": len(bitsets.table),
        "failed_tests": failed,
        "missing_recordings": missing,
        "lines_with_own_coverage": own_line_hits,
        "lines_using_the_statement_union": statement_fallbacks,
        "lines_no_test_runs": dead_lines,
        "tests_in_file": {path: numbers for path, numbers in sorted(tests_in_file.items())},
    }

    OUT.mkdir(parents=True, exist_ok=True)
    archive_path = OUT / "maps.zip"
    with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        archive.writestr("meta.json", json.dumps(meta, separators=(",", ":")))
        archive.writestr("repo_paths.txt", "\n".join(repo_paths))
        archive.writestr(
            "groups.bin",
            b"".join(bitsets.table),
        )
        for relative, entry in entries.items():
            archive.writestr("f/" + relative + ".json",
                             json.dumps(entry, separators=(",", ":")))
        for relative, raw in sources.items():
            archive.writestr("src/" + relative, raw)

    (WORK / "build_report.json").write_text(json.dumps(report, indent=1))
    print(f"groups: {len(bitsets.table)} distinct test sets, "
          f"{len(bitsets.table) * bitsets.width / 1e6:.1f} MB raw", file=sys.stderr)
    print(f"files mapped: {len(entries)}, sources bundled: {len(sources)}",
          file=sys.stderr)
    total_lines = own_line_hits + statement_fallbacks + dead_lines
    print(f"lines: {own_line_hits} that run themselves, "
          f"{statement_fallbacks} covered only through their statement, "
          f"{dead_lines} no test runs  (of {total_lines})", file=sys.stderr)
    print(f"tests that did not pass alone: {len(failed)}", file=sys.stderr)
    print(f"wrote {archive_path} ({archive_path.stat().st_size / 1e6:.2f} MB)",
          file=sys.stderr)
    if failed:
        (WORK / "failed_alone.json").write_text(json.dumps(failed, indent=2))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    runner = sub.add_parser("run", help="run every test alone under coverage")
    runner.add_argument("--jobs", type=int, default=max(os.cpu_count() // 2, 1))
    runner.add_argument("--only", nargs="*", help="test numbers or node ids")
    runner.add_argument("--resume", action="store_true",
                        help="skip tests already recorded")
    runner.add_argument("--mode", choices=("coverage", "reads"), default="coverage",
                        help="coverage: the line map.  reads: data reads and "
                             "directory scans, without coverage in the way")
    runner.set_defaults(func=command_run)

    builder = sub.add_parser("build", help="build the shipped maps")
    builder.set_defaults(func=command_build)

    arguments = parser.parse_args(argv)
    return arguments.func(arguments)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
