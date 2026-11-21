"""Unit tests for the fingerprint and the statement model.

The statement model has to agree with coverage.py: coverage reports a
multi-line statement at its first line only, so every line of that statement
must map back to it.
"""

from __future__ import annotations

import ast
import sys
import textwrap
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pinpoint import astutil  # noqa: E402


def owners(source: str) -> dict[int, int]:
    return astutil.statement_owners(ast.parse(textwrap.dedent(source).lstrip("\n")))


def blocks(source: str) -> dict[int, tuple[int, int]]:
    return astutil.block_ranges(ast.parse(textwrap.dedent(source).lstrip("\n")))


class TestFingerprint(unittest.TestCase):
    def test_a_syntax_error_has_no_fingerprint(self):
        self.assertIsNone(astutil.fingerprint("def f(:\n    pass\n"))

    def test_comments_do_not_change_it(self):
        a = "x = 1\ny = 2\n"
        b = "# a comment\nx = 1\n\n# another\ny = 2\n"
        self.assertEqual(astutil.fingerprint(a), astutil.fingerprint(b))

    def test_a_comment_between_def_and_docstring_does_not_change_it(self):
        """The bap8 sample: the string is still the first statement, so still a docstring."""
        a = 'def f():\n    """Doc."""\n    return 1\n'
        b = 'def f():\n    # Keep in sync with the generator\n    """Doc."""\n    return 1\n'
        self.assertEqual(astutil.fingerprint(a), astutil.fingerprint(b))

    def test_docstrings_do_not_change_it(self):
        a = 'def f():\n    """One."""\n    return 1\n'
        b = 'def f():\n    """Something else entirely."""\n    return 1\n'
        self.assertEqual(astutil.fingerprint(a), astutil.fingerprint(b))

    def test_a_non_docstring_string_does_change_it(self):
        a = 'def f():\n    return "one"\n'
        b = 'def f():\n    return "two"\n'
        self.assertNotEqual(astutil.fingerprint(a), astutil.fingerprint(b))

    def test_commenting_code_out_changes_it(self):
        a = "def f():\n    x = 1\n    return x\n"
        b = "def f():\n    # x = 1\n    return x\n"
        self.assertNotEqual(astutil.fingerprint(a), astutil.fingerprint(b))

    def test_real_code_changes_change_it(self):
        a = "if a is b:\n    pass\n"
        b = "if a == b:\n    pass\n"
        self.assertNotEqual(astutil.fingerprint(a), astutil.fingerprint(b))

    def test_blank_lines_and_layout_do_not_change_it(self):
        a = "x = [1, 2, 3]\n"
        b = "x = [\n    1,\n    2,\n    3,\n]\n"
        self.assertEqual(astutil.fingerprint(a), astutil.fingerprint(b))


class TestStatementOwners(unittest.TestCase):
    def test_a_multi_line_table_maps_to_its_first_line(self):
        found = owners("""
            KEYWORDS = {
                "A": 1,
                "B": 2,
                "C": 3,
            }
        """)
        for line in range(1, 6):
            self.assertEqual(found[line], 1, f"line {line}")

    def test_the_innermost_statement_wins(self):
        found = owners("""
            for item in things:
                x = (
                    item
                    + 1
                )
        """)
        self.assertEqual(found[1], 1)          # the for header
        for line in range(2, 6):
            self.assertEqual(found[line], 2)   # the assignment inside it

    def test_a_multi_line_signature_maps_to_the_def(self):
        found = owners("""
            def f(
                a=1,
                b=2,
            ):
                return a
        """)
        for line in range(1, 5):
            self.assertEqual(found[line], 1)
        self.assertEqual(found[5], 5)

    def test_a_decorator_gets_its_own_anchor(self):
        found = owners("""
            @register(
                "name"
            )
            def f():
                return 1
        """)
        self.assertEqual(found[1], 1)
        self.assertEqual(found[2], 1)
        self.assertEqual(found[4], 4)

    def test_an_except_clause_is_anchored(self):
        found = owners("""
            try:
                a()
            except ValueError:
                b()
        """)
        self.assertEqual(found[1], 1)
        self.assertEqual(found[2], 2)
        self.assertEqual(found[3], 3)
        self.assertEqual(found[4], 4)


class TestBlockRanges(unittest.TestCase):
    """The value is the block's BODY, never the whole block.

    A `def` line runs at import, so a block span that included it would make
    every function look as though every importer had entered it, and an
    insertion anywhere in any function body would select the whole module.
    """

    def test_a_function_body_excludes_the_def_line(self):
        found = blocks("""
            def f():
                a = 1
                b = 2
        """)
        self.assertEqual(found[2], (2, 3))
        # The def line still finds the block, so an insertion there is not lost.
        self.assertEqual(found[1], (2, 3))

    def test_the_innermost_block_wins(self):
        found = blocks("""
            class C:
                def m(self):
                    return 1
        """)
        self.assertEqual(found[1], (2, 3))
        self.assertEqual(found[3], (3, 3))

    def test_module_level_lines_have_no_block(self):
        found = blocks("""
            x = 1
            def f():
                pass
        """)
        self.assertNotIn(1, found)
        self.assertIn(2, found)

    def test_a_decorator_belongs_to_the_block_it_decorates(self):
        found = blocks("""
            @deco
            def f():
                pass
        """)
        self.assertEqual(found[1], (3, 3))


class TestAgainstTheRealCheckout(unittest.TestCase):
    """Every line in a real sqlglot file must have an owner inside its statement."""

    def test_sqlglot_files_map_cleanly(self):
        checkout = REPO / "vendor" / "sqlglot"
        if not checkout.is_dir():
            self.skipTest("the sqlglot checkout is not present")
        for name in ("sqlglot/parser.py", "sqlglot/dialects/dialect.py",
                     "sqlglot/generators/sqlite.py", "tests/test_optimizer.py"):
            source = (checkout / name).read_text()
            tree = ast.parse(source)
            found = astutil.statement_owners(tree)
            self.assertTrue(found, name)
            # Every anchor is its own owner: the model is idempotent.
            for line, anchor in found.items():
                self.assertLessEqual(anchor, line, f"{name}:{line}")
                self.assertEqual(found[anchor], anchor, f"{name}:{line} -> {anchor}")


if __name__ == "__main__":
    unittest.main()
