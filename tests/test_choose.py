"""Unit tests for the selection rules, against a hand-built map.

A small synthetic map keeps every rule readable and lets us assert the exact
answer.  The fingerprints are computed with the running interpreter, so this
does not depend on the Python version the real maps were built with.
"""

from __future__ import annotations

import json
import sys
import unittest
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pinpoint import astutil, choose  # noqa: E402
from pinpoint.mapstore import MapStore, bits_to_numbers  # noqa: E402

TEST_COUNT = 8
WIDTH = (TEST_COUNT + 7) // 8

MODULE_SOURCE = '''\
"""A module docstring."""

import os

TABLE = {
    "a": 1,
    "b": 2,
    "c": 3,
}


def used(value):
    """Doc."""
    return value + 1


def never_called(value):
    return value - 1
'''

DATA_FILE = "tests/fixtures/thing.sql"
MODULE = "sqlglot/thing.py"
TEST_FILE = "tests/test_thing.py"


def bitset(numbers) -> bytes:
    raw = bytearray(WIDTH)
    for number in numbers:
        raw[number >> 3] |= 1 << (number & 7)
    return bytes(raw)


class Builder:
    def __init__(self):
        self.groups: list[bytes] = []

    def add(self, numbers) -> int:
        value = bitset(numbers)
        if value in self.groups:
            return self.groups.index(value)
        self.groups.append(value)
        return len(self.groups) - 1


def build_store(tmp: Path) -> MapStore:
    builder = Builder()
    source = MODULE_SOURCE
    import ast
    tree = ast.parse(source)
    owners = astutil.statement_owners(tree)
    blocks = astutil.block_ranges(tree)
    line_count = len(source.splitlines())

    # Tests 0..3 import the module.  Test 1 calls used().  Nobody calls
    # never_called().  Test 5 reads the data file.  Tests 6 and 7 live in the
    # test file.
    importers = {0, 1, 2, 3}
    bodies = {
        node.name: (node.body[0].lineno, node.end_lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
    }
    per_statement = {}
    for line in range(1, line_count + 1):
        anchor = owners.get(line)
        if anchor is None:
            continue
        for name, (first, last) in bodies.items():
            if first <= anchor <= last:
                per_statement[anchor] = {1} if name == "used" else set()
                break
        else:
            per_statement[anchor] = set(importers)

    def runs(per_line):
        out, previous = [], None
        for line in range(1, line_count + 2):
            value = per_line.get(line, -1)
            if value != previous:
                out.append([line, value])
                previous = value
        return out

    group_of_line = {}
    for line in range(1, line_count + 1):
        anchor = owners.get(line)
        runners = per_statement.get(anchor) if anchor else None
        group_of_line[line] = builder.add(runners) if runners else -1

    group_of_block_line = {}
    for line, span in blocks.items():
        inside = set()
        for inner in range(span[0], span[1] + 1):
            inside |= per_statement.get(owners.get(inner), set()) if owners.get(inner) else set()
        group_of_block_line[line] = builder.add(inside) if inside else -1

    module_entry = {
        "sha256": "x", "lines": line_count,
        "ast": astutil.fingerprint(source),
        "runs": runs(group_of_line),
        "block_runs": runs(group_of_block_line),
        "module": builder.add(importers),
        "openers": -1,
        "importers": builder.add(importers),
        "own_tests": -1,
    }
    data_entry = {"module": -1, "openers": builder.add({5}),
                  "importers": -1, "own_tests": -1}
    test_entry = {
        "sha256": "y", "lines": 3, "ast": astutil.fingerprint("x = 1\n"),
        "runs": [[1, builder.add({6, 7})], [4, -1]],
        "block_runs": [[1, -1]],
        "module": builder.add({6, 7}),
        "openers": -1,
        "importers": builder.add({6, 7}),
        "own_tests": builder.add({6, 7}),
    }

    meta = {
        "pinned_commit": "test",
        "test_count": TEST_COUNT,
        "bitset_width": WIDTH,
        "packages": {"sqlglot": builder.add(importers)},
        # Test 5 watches the fixtures directory; test 4 watches sqlglot/typing.
        "directory_watchers": {"tests/fixtures": builder.add({5}),
                               "sqlglot/typing": builder.add({4})},
        # Tests 0..3 tried to import sqlglot._version and the import failed.
        "absent_modules": {"sqlglot._version": builder.add(importers)},
    }
    repo_paths = [MODULE, DATA_FILE, TEST_FILE, "README.md",
                  "sqlglot/typing/__init__.py"]

    path = tmp / "maps.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("meta.json", json.dumps(meta))
        archive.writestr("repo_paths.txt", "\n".join(repo_paths))
        archive.writestr("groups.bin", b"".join(builder.groups))
        archive.writestr("f/" + MODULE + ".json", json.dumps(module_entry))
        archive.writestr("f/" + DATA_FILE + ".json", json.dumps(data_entry))
        archive.writestr("f/" + TEST_FILE + ".json", json.dumps(test_entry))
        archive.writestr("src/" + MODULE, MODULE_SOURCE)
        archive.writestr("src/" + TEST_FILE, "x = 1\n")
    return MapStore(path)


def diff_for(path: str, old: str, new: str, **kwargs) -> str:
    import difflib
    status = kwargs.get("status", "modified")
    header = f"diff --git a/{path} b/{path}\n"
    if status == "added":
        header += "new file mode 100644\n"
        labels = ("/dev/null", f"b/{path}")
    elif status == "deleted":
        header += "deleted file mode 100644\n"
        labels = (f"a/{path}", "/dev/null")
    else:
        labels = (f"a/{path}", f"b/{path}")
    body = "".join(difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True),
        fromfile=labels[0], tofile=labels[1], n=3))
    return header + body


class SelectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import tempfile
        cls._tmp = tempfile.TemporaryDirectory()
        cls.store = build_store(Path(cls._tmp.name))
        cls.everything = cls.store.all_tests()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def chosen(self, patch: str) -> list[int]:
        return bits_to_numbers(choose.select(patch, self.store))

    # -- the harmless cases ------------------------------------------------

    def test_a_comment_selects_nothing(self):
        new = MODULE_SOURCE.replace("def used(value):", "# a note\ndef used(value):")
        self.assertEqual(self.chosen(diff_for(MODULE, MODULE_SOURCE, new)), [])

    def test_a_docstring_edit_selects_nothing(self):
        new = MODULE_SOURCE.replace('"""Doc."""', '"""Quite a different sentence."""')
        self.assertEqual(self.chosen(diff_for(MODULE, MODULE_SOURCE, new)), [])

    def test_a_blank_line_selects_nothing(self):
        new = MODULE_SOURCE.replace("import os\n", "import os\n\n")
        self.assertEqual(self.chosen(diff_for(MODULE, MODULE_SOURCE, new)), [])

    def test_a_file_nothing_reads_selects_nothing(self):
        patch = diff_for("README.md", "one\n", "two\n")
        self.assertEqual(self.chosen(patch), [])

    def test_a_dead_function_body_selects_nothing(self):
        new = MODULE_SOURCE.replace("return value - 1", "return value - 2")
        self.assertEqual(self.chosen(diff_for(MODULE, MODULE_SOURCE, new)), [])

    # -- the ordinary cases ------------------------------------------------

    def test_a_live_function_body_selects_its_callers(self):
        new = MODULE_SOURCE.replace("return value + 1", "return value + 2")
        self.assertEqual(self.chosen(diff_for(MODULE, MODULE_SOURCE, new)), [1])

    def test_a_table_entry_selects_every_importer(self):
        """The change is nowhere near the line coverage reports, so the whole
        statement counts."""
        new = MODULE_SOURCE.replace('"b": 2,', '"b": 22,')
        self.assertEqual(self.chosen(diff_for(MODULE, MODULE_SOURCE, new)), [0, 1, 2, 3])

    def test_an_insertion_in_a_function_selects_that_block(self):
        new = MODULE_SOURCE.replace("    return value + 1",
                                    "    value = value\n    return value + 1")
        self.assertEqual(self.chosen(diff_for(MODULE, MODULE_SOURCE, new)), [1])

    def test_an_insertion_at_module_level_selects_the_module(self):
        new = MODULE_SOURCE.replace("import os\n", "import os\nEXTRA = 1\n")
        self.assertEqual(self.chosen(diff_for(MODULE, MODULE_SOURCE, new)), [0, 1, 2, 3])

    def test_a_data_file_selects_its_readers(self):
        self.assertEqual(self.chosen(diff_for(DATA_FILE, "SELECT 1;\n", "SELECT 2;\n")), [5])

    # -- the dangerous cases -----------------------------------------------

    def test_a_syntax_error_selects_every_importer(self):
        new = MODULE_SOURCE.replace("def used(value):", "def used(value:")
        self.assertEqual(self.chosen(diff_for(MODULE, MODULE_SOURCE, new)), [0, 1, 2, 3])

    def test_a_syntax_error_in_a_test_file_selects_its_tests(self):
        patch = diff_for(TEST_FILE, "x = 1\n", "x = (\n")
        self.assertEqual(self.chosen(patch), [6, 7])

    def test_a_deleted_module_selects_everything_touching_it(self):
        patch = diff_for(MODULE, MODULE_SOURCE, "", status="deleted")
        self.assertEqual(self.chosen(patch), [0, 1, 2, 3])

    def test_a_deleted_data_file_selects_its_readers(self):
        patch = diff_for(DATA_FILE, "SELECT 1;\n", "", status="deleted")
        self.assertEqual(self.chosen(patch), [5])

    def test_a_new_file_selects_the_tests_watching_its_directory(self):
        patch = diff_for("sqlglot/typing/brand_new.py", "", "X = 1\n", status="added")
        self.assertEqual(self.chosen(patch), [4])

    def test_a_new_file_that_makes_a_failed_import_succeed(self):
        """sqlglot/__init__.py imports sqlglot._version in a try/except at the
        pinned commit, and the import fails.  Adding the file changes that."""
        patch = diff_for("sqlglot/_version.py", "", '__version__ = "1"\n',
                         status="added")
        self.assertEqual(self.chosen(patch), [0, 1, 2, 3])

    def test_a_new_file_nobody_watches_selects_nothing(self):
        patch = diff_for("sqlglot/elsewhere/brand_new.py", "", "X = 1\n", status="added")
        self.assertEqual(self.chosen(patch), [])

    def test_a_new_conftest_selects_everything(self):
        patch = diff_for("tests/conftest.py", "", "x = 1\n", status="added")
        self.assertEqual(len(self.chosen(patch)), TEST_COUNT)

    def test_an_unknown_file_selects_everything(self):
        patch = diff_for("some/unknown/place.py", "a\n", "b\n")
        self.assertEqual(len(self.chosen(patch)), TEST_COUNT)

    def test_a_binary_change_to_a_known_file_is_conservative(self):
        patch = (f"diff --git a/{MODULE} b/{MODULE}\n"
                 f"Binary files a/{MODULE} and b/{MODULE} differ\n")
        self.assertEqual(self.chosen(patch), [0, 1, 2, 3])

    def test_a_rename_counts_as_a_removal_and_an_addition(self):
        patch = (f"diff --git a/{MODULE} b/sqlglot/typing/moved.py\n"
                 "similarity index 100%\n"
                 f"rename from {MODULE}\n"
                 "rename to sqlglot/typing/moved.py\n")
        self.assertEqual(self.chosen(patch), [0, 1, 2, 3, 4])

    def test_a_context_mismatch_raises_so_the_caller_falls_back(self):
        patch = diff_for(MODULE, "completely different\nfile content\n",
                         "completely different\nfile CONTENT\n")
        with self.assertRaises(Exception):
            choose.select(patch, self.store)

    def test_several_files_union(self):
        patch = (diff_for(DATA_FILE, "SELECT 1;\n", "SELECT 2;\n")
                 + diff_for(MODULE, MODULE_SOURCE,
                            MODULE_SOURCE.replace("return value + 1", "return value + 9")))
        self.assertEqual(self.chosen(patch), [1, 5])


if __name__ == "__main__":
    unittest.main()
