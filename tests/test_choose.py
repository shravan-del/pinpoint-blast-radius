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


def used(value, extra=True):
    """Doc."""
    return value + 1


def never_called(value, extra=True):
    return value - 1
'''

CLASS_SOURCE = '''\
from sqlglot import helper, make_it


class Widget:
    TABLE = {
        "a": 1,
        "b": 2,
    }
    FLAG = True

    def render(self):
        return self.TABLE["a"]

    MADE = make_it("x", flag=True)

    class Inner:
        DEPTH = 3


def names_it():
    return Widget.TABLE
'''

DATA_FILE = "tests/fixtures/thing.sql"
MODULE = "sqlglot/thing.py"
TEST_FILE = "tests/test_thing.py"
CLASS_MODULE = "sqlglot/widget.py"


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
        "def_headers": runs(dict.fromkeys(
            astutil.narrowable_def_headers(tree), 1)),
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
        "have_class_pass": True,
        # Only test 1 ever built a Widget; nobody built a Widget.Inner.
        "class_users": {"sqlglot.widget:Widget": builder.add({1})},
        # Line 20 (`return Widget.TABLE`) is the only mention of the name, and
        # test 3 ran it.
        "name_refs": {"Widget": builder.add({3})},
        "untrackable_classes": ["sqlglot.widget:Widget.Inner"],
    }
    repo_paths = [MODULE, DATA_FILE, TEST_FILE, CLASS_MODULE, "README.md",
                  "sqlglot/typing/__init__.py"]

    # A second module with a class body, for the class-usage rule.
    ctree = ast.parse(CLASS_SOURCE)
    cowners = astutil.statement_owners(ctree)
    cblocks = astutil.block_ranges(ctree)
    in_class = astutil.class_body_lines(ctree)
    method_spans = astutil.method_body_lines(ctree)
    clines = len(CLASS_SOURCE.splitlines())
    class_names = sorted(set(in_class.values()))
    cindex = {n: i for i, n in enumerate(class_names)}

    # Tests 0..3 import it.  Test 2 ran Widget.render.  Test 3 ran names_it.
    cstmt = {}
    render_span = method_spans.get("Widget", [(0, 0)])[0]
    for line in range(1, clines + 1):
        anchor = cowners.get(line)
        if anchor is None:
            continue
        if render_span[0] <= anchor <= render_span[1]:
            cstmt[anchor] = {2}
        elif CLASS_SOURCE.splitlines()[anchor - 1].startswith("    return Widget.TABLE"):
            cstmt[anchor] = {3}
        else:
            cstmt[anchor] = set(importers)

    cgroup_line = {}
    for line in range(1, clines + 1):
        a = cowners.get(line)
        r = cstmt.get(a) if a else None
        cgroup_line[line] = builder.add(r) if r else -1
    cgroup_block = {}
    for line, span in cblocks.items():
        inside = set()
        for inner in range(span[0], span[1] + 1):
            a = cowners.get(inner)
            if a:
                inside |= cstmt.get(a, set())
        cgroup_block[line] = builder.add(inside) if inside else -1

    method_groups = []
    for name in class_names:
        ran = set()
        for st, en in method_spans.get(name, ()):
            for line in range(st, en + 1):
                a = cowners.get(line)
                if a:
                    ran |= cstmt.get(a, set())
        method_groups.append(builder.add(ran) if ran else -1)

    class_entry = {
        "def_headers": runs(dict.fromkeys(
            astutil.narrowable_def_headers(ctree), 1)),
        "sha256": "z", "lines": clines,
        "ast": astutil.fingerprint(CLASS_SOURCE),
        "runs": runs(cgroup_line),
        "block_runs": runs(cgroup_block),
        "classes": class_names,
        "class_runs": runs({
            line: cindex[n] for line, n in in_class.items()
            if line in astutil.narrowable_class_lines(ctree)
        }),
        "class_methods": method_groups,
        "module": builder.add(importers),
        "openers": -1,
        "importers": builder.add(importers),
        "own_tests": -1,
    }

    path = tmp / "maps.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("meta.json", json.dumps(meta))
        archive.writestr("repo_paths.txt", "\n".join(repo_paths))
        archive.writestr("groups.bin", b"".join(builder.groups))
        archive.writestr("f/" + MODULE + ".json", json.dumps(module_entry))
        archive.writestr("f/" + DATA_FILE + ".json", json.dumps(data_entry))
        archive.writestr("f/" + TEST_FILE + ".json", json.dumps(test_entry))
        archive.writestr("src/" + MODULE, MODULE_SOURCE)
        archive.writestr("f/" + CLASS_MODULE + ".json", json.dumps(class_entry))
        archive.writestr("src/" + CLASS_MODULE, CLASS_SOURCE)
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
        new = MODULE_SOURCE.replace("def used(", "# a note\ndef used(")
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
        new = MODULE_SOURCE.replace("def used(value, extra=True):", "def used(value:")
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

    # -- the class-usage rule ----------------------------------------------

    def test_a_class_body_value_selects_only_the_users_of_that_class(self):
        """The ktz3 case: one entry of a table in a class body.  Every importer
        runs that statement, so only class usage separates them."""
        new = CLASS_SOURCE.replace('"b": 2,', '"b": 22,')
        # test 1 built a Widget, test 2 ran Widget.render, test 3 names Widget.
        self.assertEqual(self.chosen(diff_for(CLASS_MODULE, CLASS_SOURCE, new)),
                         [1, 2, 3])

    def test_a_class_body_change_that_renames_a_key_does_not_narrow(self):
        """A changed string can be looked up by name from anywhere, so the
        class-usage evidence does not bound it and we keep every importer."""
        new = CLASS_SOURCE.replace('"b": 2,', '"bb": 2,')
        self.assertEqual(self.chosen(diff_for(CLASS_MODULE, CLASS_SOURCE, new)),
                         [0, 1, 2, 3])

    def test_a_class_body_change_in_an_untrackable_class_does_not_narrow(self):
        new = CLASS_SOURCE.replace("DEPTH = 3", "DEPTH = 4")
        self.assertEqual(self.chosen(diff_for(CLASS_MODULE, CLASS_SOURCE, new)),
                         [0, 1, 2, 3])

    def test_a_method_body_still_uses_line_coverage(self):
        new = CLASS_SOURCE.replace('return self.TABLE["a"]', 'return self.TABLE["b"]')
        self.assertEqual(self.chosen(diff_for(CLASS_MODULE, CLASS_SOURCE, new)), [2])

    def test_module_level_outside_a_class_still_selects_every_importer(self):
        new = CLASS_SOURCE.replace("from sqlglot import helper",
                                   "from sqlglot import helper\nEXTRA = 1")
        self.assertEqual(self.chosen(diff_for(CLASS_MODULE, CLASS_SOURCE, new)),
                         [0, 1, 2, 3])

    def test_a_class_body_call_does_not_narrow(self):
        """`MADE = make_it("x", flag=True)` runs at import, and the call may do
        anything, so class usage does not bound it."""
        new = CLASS_SOURCE.replace("flag=True", "flag=False")
        self.assertEqual(self.chosen(diff_for(CLASS_MODULE, CLASS_SOURCE, new)),
                         [0, 1, 2, 3])

    def test_adding_an_unused_helper_selects_nothing(self):
        new = MODULE_SOURCE + "\n\ndef brand_new_helper(value):\n    return value\n"
        self.assertEqual(self.chosen(diff_for(MODULE, MODULE_SOURCE, new)), [])

    def test_adding_a_helper_that_shadows_a_builtin_is_not_inert(self):
        new = MODULE_SOURCE + "\n\ndef len(value):\n    return 0\n"
        self.assertEqual(self.chosen(diff_for(MODULE, MODULE_SOURCE, new)), [0, 1, 2, 3])

    def test_adding_a_decorated_helper_is_not_inert(self):
        new = MODULE_SOURCE + "\n\n@os.register\ndef brand_new(value):\n    return value\n"
        self.assertEqual(self.chosen(diff_for(MODULE, MODULE_SOURCE, new)), [0, 1, 2, 3])

    def test_adding_a_class_is_not_inert(self):
        """A class can register itself through a metaclass or
        __init_subclass__, so adding one is not a no-op."""
        new = MODULE_SOURCE + "\n\nclass BrandNew:\n    pass\n"
        self.assertEqual(self.chosen(diff_for(MODULE, MODULE_SOURCE, new)), [0, 1, 2, 3])

    def test_adding_an_import_is_not_inert(self):
        new = MODULE_SOURCE.replace("import os\n", "import os\nimport sys\n")
        self.assertEqual(self.chosen(diff_for(MODULE, MODULE_SOURCE, new)), [0, 1, 2, 3])

    def test_a_default_argument_selects_the_callers(self):
        """A default is read when the function is called, so only a test that
        reached the body can notice."""
        new = MODULE_SOURCE.replace("def used(value, extra=True):",
                                    "def used(value, extra=False):")
        self.assertEqual(self.chosen(diff_for(MODULE, MODULE_SOURCE, new)), [1])

    def test_a_default_argument_on_a_dead_function_selects_nothing(self):
        new = MODULE_SOURCE.replace("def never_called(value, extra=True):",
                                    "def never_called(value, extra=False):")
        self.assertEqual(self.chosen(diff_for(MODULE, MODULE_SOURCE, new)), [])

    def test_renaming_a_parameter_does_not_narrow(self):
        """A caller using that keyword fails at the call, before the body, so
        "the tests that ran the body" does not bound it."""
        new = MODULE_SOURCE.replace("def used(value, extra=True):",
                                    "def used(value, other=True):")
        self.assertEqual(self.chosen(diff_for(MODULE, MODULE_SOURCE, new)),
                         [0, 1, 2, 3])

    def test_several_files_union(self):
        patch = (diff_for(DATA_FILE, "SELECT 1;\n", "SELECT 2;\n")
                 + diff_for(MODULE, MODULE_SOURCE,
                            MODULE_SOURCE.replace("return value + 1", "return value + 9")))
        self.assertEqual(self.chosen(patch), [1, 5])


if __name__ == "__main__":
    unittest.main()
