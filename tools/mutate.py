#!/usr/bin/env python3
"""Generate and label local mutants, to measure the selector beyond 30 samples.

EVALUATION ONLY.  Nothing this produces is shipped, and nothing in the entry is
keyed on it: the mutants exist so we can measure misses on kinds of change the
sample set never had.  The selector never sees a mutant, only a patch.

  generate   write N single-change patches against the pinned commit
  label      run the real test suite on each one and record what failed
  score      score the selector against the labels, broken down by kind

Run inside the lab image.
"""

from __future__ import annotations

import argparse
import ast
import difflib
import json
import os
import random
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

SQLGLOT_ROOT = Path(os.environ.get("PINPOINT_SQLGLOT_ROOT", "/src/sqlglot"))
REPO = Path(os.environ.get("PINPOINT_REPO", "/work"))
EVAL = Path(os.environ.get("PINPOINT_EVAL", "/work/eval/mutants"))
TESTS_JSON = Path(os.environ.get("PINPOINT_TESTS_JSON", "/work/sample/tests.json"))
SCRATCH = Path(os.environ.get("PINPOINT_SCRATCH", "/tmp/pinpoint-mutants"))
POOL_CPUS = int(os.environ.get("PINPOINT_POOL_CPUS", "2"))

# Tests whose work happens in a spawned pool, so their result depends on how
# busy the machine is.  They are re-run alone for every mutant.
LOAD_SENSITIVE = (
    "tests/test_optimizer.py::TestOptimizer::test_tpch",
    "tests/test_optimizer.py::TestOptimizer::test_tpcds",
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _read(path: str) -> str:
    return (SQLGLOT_ROOT / path).read_text("utf-8")


def _py_files(prefix: str) -> list[str]:
    return sorted(
        str(p.relative_to(SQLGLOT_ROOT))
        for p in (SQLGLOT_ROOT / prefix).rglob("*.py")
    )


def make_patch(path: str, old: str, new: str, *, status: str = "modified",
               new_path: str | None = None) -> str:
    """A unified diff in the shape git produces, which is what /data holds."""
    old_lines = old.splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    target = new_path or path
    header = f"diff --git a/{path} b/{target}\n"
    if status == "added":
        header += "new file mode 100644\n"
        from_label, to_label = "/dev/null", f"b/{target}"
    elif status == "deleted":
        header += "deleted file mode 100644\n"
        from_label, to_label = f"a/{path}", "/dev/null"
    else:
        header += "index 0000000..1111111 100644\n"
        from_label, to_label = f"a/{path}", f"b/{target}"
    body = "".join(
        difflib.unified_diff(old_lines, new_lines,
                             fromfile=from_label, tofile=to_label, n=3)
    )
    if not body:
        return ""
    if not body.endswith("\n"):
        body += "\n\\ No newline at end of file\n"
    return header + body


def _replace_line(text: str, index: int, replacement: str | None) -> str:
    lines = text.split("\n")
    if replacement is None:
        del lines[index]
    else:
        lines[index] = replacement
    return "\n".join(lines)


def _indent_of(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


# --------------------------------------------------------------------------
# the mutation kinds
# --------------------------------------------------------------------------

OPERATOR_SWAPS = [
    (" == ", " != "), (" != ", " == "), (" is not ", " is "), (" is ", " is not "),
    (" < ", " <= "), (" <= ", " < "), (" > ", " >= "), (" >= ", " > "),
    (" and ", " or "), (" or ", " and "), (" + ", " - "), (" in ", " not in "),
]


def _candidate_lines(text: str) -> list[tuple[int, str]]:
    return [
        (index, line)
        for index, line in enumerate(text.split("\n"))
        if line.strip() and not line.lstrip().startswith("#")
    ]


def mutate_operator(rng: random.Random, path: str) -> tuple[str, str] | None:
    text = _read(path)
    options = [
        (index, line, old, new)
        for index, line in _candidate_lines(text)
        for old, new in OPERATOR_SWAPS
        if old in line
    ]
    if not options:
        return None
    index, line, old, new = rng.choice(options)
    return text, _replace_line(text, index, line.replace(old, new, 1))


def mutate_constant(rng: random.Random, path: str) -> tuple[str, str] | None:
    text = _read(path)
    options = []
    for index, line in _candidate_lines(text):
        for match in re.finditer(r"(?<![\w.])(\d+)(?![\w.])", line):
            options.append((index, line, match.start(), match.group(1)))
    if not options:
        return None
    index, line, start, digits = rng.choice(options)
    replacement = str(int(digits) + rng.choice([1, 2, 7]))
    changed = line[:start] + replacement + line[start + len(digits):]
    return text, _replace_line(text, index, changed)


def mutate_table_entry(rng: random.Random, path: str) -> tuple[str, str] | None:
    """Change or drop one entry in the middle of a multi-line dict or set.

    This is the dialect-table case: KEYWORDS, FUNCTIONS, TRANSFORMS and friends
    are one statement spanning hundreds of lines, so the change is nowhere near
    the line coverage reports.
    """
    text = _read(path)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    spans = [
        (node.lineno, node.end_lineno)
        for node in ast.walk(tree)
        if isinstance(node, (ast.Dict, ast.Set))
        and node.end_lineno and node.end_lineno - node.lineno >= 6
    ]
    if not spans:
        return None
    start, end = rng.choice(spans)
    lines = text.split("\n")
    inner = [
        index for index in range(start, end - 1)
        if lines[index].strip().endswith(",") and ":" in lines[index]
    ]
    if not inner:
        return None
    index = rng.choice(inner)
    if rng.random() < 0.5:
        return text, _replace_line(text, index, None)          # drop the entry
    line = lines[index]
    key_end = line.index(":")
    return text, _replace_line(text, index, line[:key_end] + ": None,")


def mutate_default_argument(rng: random.Random, path: str) -> tuple[str, str] | None:
    text = _read(path)
    options = [
        (index, line) for index, line in _candidate_lines(text)
        if re.search(r"\w+\s*(:\s*[^=]+)?=\s*(True|False|None|\d+)\s*[,)]", line)
        and ("def " in line or line.strip().endswith(","))
    ]
    if not options:
        return None
    index, line = rng.choice(options)
    for was, now in (("=True", "=False"), ("=False", "=True"), ("=None", "=()")):
        if was in line:
            return text, _replace_line(text, index, line.replace(was, now, 1))
    return None


def mutate_delete_statement(rng: random.Random, path: str) -> tuple[str, str] | None:
    text = _read(path)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    simple = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.Expr))
        and node.lineno == node.end_lineno
    ]
    if not simple:
        return None
    node = rng.choice(simple)
    lines = text.split("\n")
    index = node.lineno - 1
    return text, _replace_line(text, index, _indent_of(lines[index]) + "pass")


def mutate_docstring(rng: random.Random, path: str) -> tuple[str, str] | None:
    text = _read(path)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    holders = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                             ast.AsyncFunctionDef))
        and node.body and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    ]
    if not holders:
        return None
    node = rng.choice(holders)
    first = node.body[0]
    lines = text.split("\n")
    index = first.lineno - 1
    line = lines[index]
    marker = '"""' if '"""' in line else ("'''" if "'''" in line else None)
    if marker and line.count(marker) == 1:
        return text, _replace_line(text, index, line + " Reworded by the mutator.")
    if marker and line.count(marker) == 2:
        closing = line.rindex(marker)
        return text, _replace_line(text, index, line[:closing] + " reworded." + line[closing:])
    return None


def mutate_comment(rng: random.Random, path: str) -> tuple[str, str] | None:
    text = _read(path)
    options = _candidate_lines(text)
    if not options:
        return None
    index, line = rng.choice(options)
    lines = text.split("\n")
    lines.insert(index, _indent_of(line) + "# mutator: an explanatory comment")
    return text, "\n".join(lines)


def mutate_whitespace(rng: random.Random, path: str) -> tuple[str, str] | None:
    text = _read(path)
    options = [(i, line) for i, line in _candidate_lines(text) if not line.startswith(" ")]
    if not options:
        return None
    index, _ = rng.choice(options)
    lines = text.split("\n")
    lines.insert(index, "")
    return text, "\n".join(lines)


def mutate_import(rng: random.Random, path: str) -> tuple[str, str] | None:
    text = _read(path)
    lines = text.split("\n")
    options = [i for i, line in enumerate(lines)
               if line.startswith("import ") or line.startswith("from ")]
    if not options:
        return None
    index = rng.choice(options)
    lines.insert(index, "import collections  # mutator: an unused import")
    return text, "\n".join(lines)


def mutate_unused_helper(rng: random.Random, path: str) -> tuple[str, str] | None:
    text = _read(path)
    return text, text.rstrip("\n") + (
        "\n\n\ndef _mutator_unused_helper(value):\n"
        "    \"\"\"Never called by anything.\"\"\"\n"
        "    return value\n"
    )


def mutate_syntax_error(rng: random.Random, path: str) -> tuple[str, str] | None:
    text = _read(path)
    options = [(i, line) for i, line in _candidate_lines(text) if line.strip().endswith(":")]
    if not options:
        return None
    index, line = rng.choice(options)
    return text, _replace_line(text, index, line.rstrip()[:-1] + " (")


def mutate_fixture(rng: random.Random, path: str) -> tuple[str, str] | None:
    text = _read(path)
    options = [(i, line) for i, line in enumerate(text.split("\n"))
               if line.strip() and not line.startswith("#") and not line.startswith("--")]
    if not options:
        return None
    index, line = rng.choice(options)
    return text, _replace_line(text, index, line.replace("SELECT", "SELECT 1", 1)
                               if "SELECT" in line else line + " 1")


def mutate_test_file(rng: random.Random, path: str) -> tuple[str, str] | None:
    text = _read(path)
    options = [(i, line) for i, line in _candidate_lines(text)
               if "assertEqual" in line or "self.validate_identity" in line
               or "self.validate_all" in line]
    if not options:
        return None
    index, line = rng.choice(options)
    if '"' in line:
        at = line.index('"')
        return text, _replace_line(text, index, line[:at + 1] + "ZZ" + line[at + 1:])
    return None


def mutate_delete_line(rng: random.Random, path: str) -> tuple[str, str] | None:
    text = _read(path)
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return None
    bodies = [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and len(node.body) > 2
    ]
    if not bodies:
        return None
    node = rng.choice(bodies)
    victim = rng.choice(node.body[:-1])
    if victim.lineno != victim.end_lineno:
        return None
    return text, _replace_line(text, victim.lineno - 1, None)


NEW_FILE_BODIES = {
    "plain": "VALUE = 1\n\n\ndef helper():\n    return VALUE\n",
    "typing": (
        "from sqlglot.typing.dialect import EXPRESSION_METADATA as _PARENT\n\n"
        "EXPRESSION_METADATA = {**_PARENT}\n"
    ),
}


def mutate_new_file(rng: random.Random, path: str) -> tuple[str, str] | None:
    return None  # handled separately; a new file has no old version


# --------------------------------------------------------------------------
# generate
# --------------------------------------------------------------------------

SQLGLOT_AREAS = {
    "dialects": "sqlglot/dialects",
    "parsers": "sqlglot/parsers",
    "generators": "sqlglot/generators",
    "typing": "sqlglot/typing",
    "optimizer": "sqlglot/optimizer",
    "executor": "sqlglot/executor",
    "expressions": "sqlglot/expressions",
    "core": "sqlglot",
}

# Weighted toward what the sample set showed, with every other kind present
# because the final set is promised to hold kinds we have not seen.
PLAN = [
    ("operator", mutate_operator, "sqlglot", 14),
    ("constant", mutate_constant, "sqlglot", 8),
    ("table_entry", mutate_table_entry, "sqlglot", 12),
    ("executor", mutate_operator, "sqlglot/executor", 6),
    ("executor_delete", mutate_delete_statement, "sqlglot/executor", 4),
    ("default_argument", mutate_default_argument, "sqlglot", 5),
    ("delete_statement", mutate_delete_statement, "sqlglot", 7),
    ("delete_line", mutate_delete_line, "sqlglot", 5),
    ("docstring", mutate_docstring, "sqlglot", 6),
    ("comment", mutate_comment, "sqlglot", 6),
    ("whitespace", mutate_whitespace, "sqlglot", 4),
    ("import", mutate_import, "sqlglot", 4),
    ("unused_helper", mutate_unused_helper, "sqlglot", 4),
    ("syntax_error", mutate_syntax_error, "sqlglot", 4),
    ("fixture", mutate_fixture, "FIXTURES", 10),
    ("test_edit", mutate_test_file, "tests", 8),
    ("test_comment", mutate_comment, "tests", 3),
]


def _pool(area: str, rng: random.Random) -> list[str]:
    if area == "FIXTURES":
        return sorted(
            str(p.relative_to(SQLGLOT_ROOT))
            for p in (SQLGLOT_ROOT / "tests/fixtures").rglob("*")
            if p.is_file() and p.suffix in (".sql", ".json")
        )
    if area == "tests":
        return [p for p in _py_files("tests")
                if "/fixtures/" not in p and p.rsplit("/", 1)[-1].startswith("test_")]
    return [p for p in _py_files(area) if "/__pycache__/" not in p]


def command_generate(arguments: argparse.Namespace) -> int:
    rng = random.Random(arguments.seed)
    commits = EVAL / "commits"
    if commits.exists():
        shutil.rmtree(commits)
    commits.mkdir(parents=True)

    scale = arguments.count / sum(weight for _, _, _, weight in PLAN)
    manifest: dict[str, dict] = {}
    serial = 0

    for kind, mutator, area, weight in PLAN:
        pool = _pool(area, rng)
        wanted = max(1, round(weight * scale))
        made = 0
        attempts = 0
        while made < wanted and attempts < wanted * 40:
            attempts += 1
            path = rng.choice(pool)
            try:
                produced = mutator(rng, path)
            except Exception:  # noqa: BLE001 - a mutator that does not fit, skip
                produced = None
            if not produced:
                continue
            old, new = produced
            if old == new:
                continue
            patch = make_patch(path, old, new)
            if not patch:
                continue
            serial += 1
            commit_id = f"m{serial:04d}"
            (commits / f"{commit_id}.patch").write_text(patch)
            manifest[commit_id] = {"kind": kind, "path": path}
            made += 1

    # New files: no old version, so they are built here rather than by a mutator.
    for name, body in NEW_FILE_BODIES.items():
        for target in (f"sqlglot/typing/mutator_{name}.py",
                       f"sqlglot/mutator_{name}.py",
                       f"tests/fixtures/optimizer/mutator_{name}.sql"):
            serial += 1
            commit_id = f"m{serial:04d}"
            text = body if target.endswith(".py") else "SELECT 1;\nSELECT 1;\n"
            (commits / f"{commit_id}.patch").write_text(
                make_patch(target, "", text, status="added"))
            manifest[commit_id] = {"kind": "new_file", "path": target}

    # Deleted files.
    for target in ("sqlglot/typing/dremio.py", "tests/fixtures/optimizer/tpch.sql",
                   "sqlglot/generators/drill.py"):
        if not (SQLGLOT_ROOT / target).exists():
            continue
        serial += 1
        commit_id = f"m{serial:04d}"
        (commits / f"{commit_id}.patch").write_text(
            make_patch(target, _read(target), "", status="deleted"))
        manifest[commit_id] = {"kind": "deleted_file", "path": target}

    (EVAL / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True))
    shutil.copy(TESTS_JSON, EVAL / "tests.json")
    by_kind: dict[str, int] = {}
    for entry in manifest.values():
        by_kind[entry["kind"]] = by_kind.get(entry["kind"], 0) + 1
    print(f"wrote {len(manifest)} mutants to {commits}", file=sys.stderr)
    for kind in sorted(by_kind):
        print(f"  {kind:18s} {by_kind[kind]}", file=sys.stderr)
    return 0


# --------------------------------------------------------------------------
# label: the real test suite decides
# --------------------------------------------------------------------------

# pytest 9's short summary.  Three shapes matter and the first one is a trap:
#
#   SUBFAILED[127, (x + 1) + 2] tests/test_optimizer.py::TestOptimizer::test_simplify
#   FAILED tests/dialects/test_dremio.py::TestDremio::test_regexp_split - ...
#   ERROR tests/test_thing.py - SyntaxError: ...
#
# A subtest failure writes the marker with no space before a bracket that
# itself contains spaces, and pytest also counts the test as passed, so a
# regex expecting whitespace after the marker drops every subtest failure
# silently.  sqlglot runs 25440 subtests, so that is most of the signal.
SUMMARY_MARKER = re.compile(r"^(FAILED|ERROR|SUBFAILED|SUBFAIL)\b")
SUMMARY_NODE_ID = re.compile(r"(tests/\S+?\.py::\S+)")
SUMMARY_FILE = re.compile(r"(tests/\S+?\.py)")


def _patched_paths(patch_text: str) -> list[str]:
    """The files a patch touches, read straight off its headers.

    Both sides matter: a deletion has `+++ /dev/null`, so looking only at the
    new side finds nothing and the "did the mutation land" check wrongly
    rejects every deleted-file mutant.
    """
    found = []
    for line in patch_text.split("\n"):
        if line.startswith(("+++ ", "--- ")):
            name = line[4:].strip()
            if name != "/dev/null":
                found.append(name[2:] if name.startswith(("a/", "b/")) else name)
    return found


def _run_suite(work: Path, environment: dict, parallel: list) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--no-header", "-p", "no:cacheprovider",
         "-rfE", "--tb=no", "--timeout", "300",
         # Without this, one un-importable test file aborts the whole run
         # ("Interrupted: 1 error during collection") and nothing else is
         # executed, so a syntax-error mutant would be labeled with only the
         # collection error and every other test left unknown.
         "--continue-on-collection-errors",
         *parallel, "tests"],
        cwd=str(work), env=environment, capture_output=True, text=True, timeout=1200,
    )


def _label_one(job: tuple) -> dict:
    commit_id, patch_path, jobs = job
    work = SCRATCH / commit_id
    if work.exists():
        shutil.rmtree(work)
    work.parent.mkdir(parents=True, exist_ok=True)
    # A real copy, never hard links.  git apply and patch rewrite in place, so
    # a shared inode would let one mutant corrupt the pinned checkout and every
    # other mutant running beside it - which is exactly what happened the first
    # time this was written, and it made every label meaningless.
    shutil.copytree(SQLGLOT_ROOT, work, symlinks=True)

    patch_text = Path(patch_path).read_text("utf-8", errors="replace")
    applied = subprocess.run(
        ["git", "apply", "-p1", "--unsafe-paths", str(patch_path)],
        cwd=str(work), capture_output=True, text=True,
    )
    if applied.returncode != 0:
        shutil.rmtree(work, ignore_errors=True)
        return {"commit": commit_id, "error": "could not apply",
                "detail": (applied.stderr or applied.stdout)[:400]}

    # Prove the mutation really landed.  A patch that quietly applied to
    # nothing would label the pinned commit instead of the mutant, and every
    # number downstream of that would be a lie.
    landed = False
    for relative in _patched_paths(patch_text):
        pristine = SQLGLOT_ROOT / relative
        mutated = work / relative
        if not mutated.exists() or not pristine.exists():
            landed = True
            break
        if pristine.read_bytes() != mutated.read_bytes():
            landed = True
            break
    if not landed:
        shutil.rmtree(work, ignore_errors=True)
        return {"commit": commit_id, "error": "patch applied but changed nothing",
                "detail": ""}

    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(work)
    environment["SKIP_INTEGRATION"] = "0"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    # tests/test_optimizer.py builds a ProcessPoolExecutor sized by
    # os.process_cpu_count(), so several mutants at once means dozens of spawned
    # interpreters and an out-of-memory kill or, worse, a pool that quietly
    # degrades and lets test_tpch and test_tpcds pass when they should fail.
    # Capping the count keeps each mutant's own fan-out small enough that
    # labeling several at a time still gives the answer a lone run gives.
    environment["PYTHON_CPU_COUNT"] = str(POOL_CPUS)
    # Serial on purpose.  Under `pytest -n 2` roughly ten tests fail that pass
    # alone - test_lazy_load, test_agg_order and friends depend on what else
    # has run in the same worker - and that noise swamps the real signal.
    # Serially the pinned checkout is 1620 passed, 0 failed, in 87 s.
    parallel = ["-n", str(jobs)] if jobs > 1 else []
    started = time.monotonic()
    try:
        finished = _run_suite(work, environment, parallel)
    except subprocess.TimeoutExpired:
        # A mutant that hangs the whole suite is a real outcome, but it is not
        # a label: we never learn which tests failed.  Record it as a problem
        # rather than letting the exception take the whole run down.
        shutil.rmtree(work, ignore_errors=True)
        return {"commit": commit_id, "error": "suite timed out", "detail": ""}
    elapsed = time.monotonic() - started

    failing: set[str] = set()
    broken_files: set[str] = set()
    for line in (finished.stdout or "").splitlines():
        line = line.strip()
        if not SUMMARY_MARKER.match(line):
            continue
        node = SUMMARY_NODE_ID.search(line)
        if node:
            failing.add(node.group(1))
            continue
        # An ERROR with no node id is a whole file that could not be
        # collected: every test in it counts as failed.
        whole_file = SUMMARY_FILE.search(line)
        if whole_file:
            broken_files.add(whole_file.group(1))

    # Two tests fan their work out to a spawned ProcessPoolExecutor, and with
    # several mutants being labeled at once they come back green on a commit
    # that a lone run shows them failing.  Validation against the 30 official
    # answers caught exactly these two and nothing else.  Re-running them by
    # themselves costs a few seconds and removes the doubt; an under-reported
    # failure here would quietly excuse a real miss.
    for node_id in LOAD_SENSITIVE:
        try:
            lone = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "--no-header",
                 "-p", "no:cacheprovider", "-rfE", "--tb=no", "--timeout", "300",
                 node_id],
                cwd=str(work), env=environment, capture_output=True, text=True,
                timeout=900,
            )
        except subprocess.TimeoutExpired:
            failing.add(node_id)
            continue
        if lone.returncode == 1:
            failing.add(node_id)
        elif lone.returncode == 0:
            failing.discard(node_id)
        elif node_id.split("::")[0] in broken_files or lone.returncode in (2, 4):
            # The file cannot be collected at all, which is the mutation doing
            # its job: the full run already recorded the whole file as broken,
            # so leave that verdict alone rather than discarding the mutant.
            pass
        else:
            shutil.rmtree(work, ignore_errors=True)
            return {"commit": commit_id,
                    "error": f"re-run of {node_id} exited {lone.returncode}",
                    "detail": (lone.stdout or "")[-400:]}

    shutil.rmtree(work, ignore_errors=True)

    # pytest exits 0 when everything passed and 1 when something failed.
    # Anything else - 2 for a usage or collection abort, -9 for an OOM kill -
    # means the run did not finish, and its empty failure list is not evidence
    # that nothing failed.  Refusing to label is the only safe answer: a killed
    # run that quietly labels a commit "breaks nothing" would teach the
    # evaluation that a real miss is fine.
    if finished.returncode not in (0, 1):
        return {"commit": commit_id, "error": f"pytest exited {finished.returncode}",
                "detail": ((finished.stdout or "")[-600:]
                           + (finished.stderr or "")[-600:])}

    return {
        "commit": commit_id,
        "returncode": finished.returncode,
        "seconds": round(elapsed, 1),
        "failing_ids": sorted(failing),
        "broken_files": sorted(broken_files),
        "tail": "",
    }


def command_label(arguments: argparse.Namespace) -> int:
    source = Path(arguments.commits) if arguments.commits else EVAL / "commits"
    out_path = Path(arguments.out) if arguments.out else EVAL / "answers.json"
    tests = json.loads(TESTS_JSON.read_text("utf-8"))["tests"]
    number_of = {test["id"]: test["number"] for test in tests}

    manifest_path = EVAL / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}

    patches = sorted(source.glob("*.patch"))
    existing = {}
    if out_path.exists() and arguments.resume:
        existing = json.loads(out_path.read_text())["commits"]
        patches = [p for p in patches if p.stem not in existing]

    print(f"labeling {len(patches)} patches, {arguments.workers} at a time, "
          f"pytest -n {arguments.jobs}", file=sys.stderr)

    answers = dict(existing)
    problems = []
    started = time.monotonic()
    done = 0
    with ProcessPoolExecutor(max_workers=arguments.workers) as pool:
        futures = [pool.submit(_label_one, (p.stem, str(p.resolve()), arguments.jobs))
                   for p in patches]
        for future in as_completed(futures):
            try:
                result = future.result()
            except Exception as error:  # noqa: BLE001
                done += 1
                problems.append({"commit": "?", "error": repr(error)[:300]})
                print(f"  a mutant blew up: {error!r}", file=sys.stderr)
                continue
            done += 1
            commit_id = result["commit"]
            if "error" in result:
                problems.append(result)
                print(f"  {commit_id}: {result['error']}", file=sys.stderr)
                continue
            failing_ids = set(result["failing_ids"])
            for broken in result.get("broken_files", ()):
                failing_ids |= {i for i in number_of if i.startswith(broken + "::")}
            unknown = [i for i in failing_ids if i not in number_of]
            numbers = sorted(number_of[i] for i in failing_ids if i in number_of)
            answers[commit_id] = {
                "failing": numbers,
                "kind": manifest.get(commit_id, {}).get("kind", "unknown"),
                "path": manifest.get(commit_id, {}).get("path", ""),
                "returncode": result["returncode"],
                "unknown_ids": unknown,
            }
            if done % 10 == 0 or done == len(patches):
                rate = done / max(time.monotonic() - started, 1e-9)
                print(f"  {done}/{len(patches)}  {rate * 60:.1f}/min", file=sys.stderr)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps({"commits": answers}, indent=1, sort_keys=True))

    print(f"labeled {len(answers)} commits in {time.monotonic() - started:.0f}s, "
          f"{len(problems)} could not be applied", file=sys.stderr)
    if problems:
        (EVAL / "label_problems.json").write_text(json.dumps(problems, indent=2))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    generator = sub.add_parser("generate")
    generator.add_argument("--count", type=int, default=120)
    generator.add_argument("--seed", type=int, default=20261120)
    generator.set_defaults(func=command_generate)

    labeler = sub.add_parser("label")
    labeler.add_argument("--workers", type=int, default=3,
                         help="mutants labeled at the same time")
    labeler.add_argument("--jobs", type=int, default=1,
                         help="pytest -n per mutant; 1 means serial, which is "
                              "the only setting that gives a clean baseline")
    labeler.add_argument("--commits", help="a directory of patches to label")
    labeler.add_argument("--out", help="where to write answers.json")
    labeler.add_argument("--resume", action="store_true")
    labeler.set_defaults(func=command_label)

    arguments = parser.parse_args(argv)
    return arguments.func(arguments)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
