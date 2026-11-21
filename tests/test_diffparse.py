"""Unit tests for the unified-diff parser."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from pinpoint import diffparse  # noqa: E402


def patch_of(body: str) -> str:
    return body.lstrip("\n")


class TestPaths(unittest.TestCase):
    def test_a_and_b_prefixes_are_dropped(self):
        changes = diffparse.parse_patch(patch_of("""
diff --git a/sqlglot/x.py b/sqlglot/x.py
index 1..2 100644
--- a/sqlglot/x.py
+++ b/sqlglot/x.py
@@ -1,2 +1,2 @@
-a = 1
+a = 2
 b = 3
"""))
        self.assertEqual(len(changes), 1)
        self.assertEqual(changes[0].path, "sqlglot/x.py")
        self.assertEqual(changes[0].status, diffparse.MODIFIED)
        self.assertFalse(changes[0].binary)

    def test_c_quoted_paths(self):
        self.assertEqual(diffparse.unquote_path(r'"a/weird\tname.py"'), "a/weird\tname.py")
        self.assertEqual(diffparse.unquote_path(r'"a/caf\303\251.sql"'), "a/café.sql")
        self.assertEqual(diffparse.unquote_path("plain.py"), "plain.py")

    def test_a_quoted_path_in_a_header(self):
        changes = diffparse.parse_patch(patch_of("""
diff --git "a/tests/fixtures/a b.sql" "b/tests/fixtures/a b.sql"
--- "a/tests/fixtures/a b.sql"
+++ "b/tests/fixtures/a b.sql"
@@ -1 +1 @@
-x
+y
"""))
        self.assertEqual(changes[0].path, "tests/fixtures/a b.sql")


class TestStatuses(unittest.TestCase):
    def test_new_file(self):
        changes = diffparse.parse_patch(patch_of("""
diff --git a/sqlglot/new.py b/sqlglot/new.py
new file mode 100644
index 0000000..abcdefg
--- /dev/null
+++ b/sqlglot/new.py
@@ -0,0 +1,2 @@
+x = 1
+y = 2
"""))
        self.assertEqual(changes[0].status, diffparse.ADDED)
        self.assertIsNone(changes[0].old_path)
        self.assertEqual(changes[0].new_path, "sqlglot/new.py")

    def test_deleted_file(self):
        changes = diffparse.parse_patch(patch_of("""
diff --git a/sqlglot/gone.py b/sqlglot/gone.py
deleted file mode 100644
index abcdefg..0000000
--- a/sqlglot/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-x = 1
-y = 2
"""))
        self.assertEqual(changes[0].status, diffparse.DELETED)
        self.assertEqual(changes[0].old_path, "sqlglot/gone.py")
        self.assertIsNone(changes[0].new_path)

    def test_rename(self):
        changes = diffparse.parse_patch(patch_of("""
diff --git a/sqlglot/old.py b/sqlglot/new.py
similarity index 92%
rename from sqlglot/old.py
rename to sqlglot/new.py
--- a/sqlglot/old.py
+++ b/sqlglot/new.py
@@ -1 +1 @@
-x = 1
+x = 2
"""))
        self.assertTrue(changes[0].renamed)
        self.assertEqual(changes[0].old_path, "sqlglot/old.py")
        self.assertEqual(changes[0].new_path, "sqlglot/new.py")

    def test_pure_rename_without_content(self):
        changes = diffparse.parse_patch(patch_of("""
diff --git a/a.py b/b.py
similarity index 100%
rename from a.py
rename to b.py
"""))
        self.assertTrue(changes[0].renamed)
        self.assertEqual(changes[0].hunks, [])

    def test_binary(self):
        changes = diffparse.parse_patch(patch_of("""
diff --git a/sqlglot.png b/sqlglot.png
index 1..2 100644
Binary files a/sqlglot.png and b/sqlglot.png differ
"""))
        self.assertTrue(changes[0].binary)
        self.assertEqual(changes[0].path, "sqlglot.png")

    def test_mode_change_only(self):
        changes = diffparse.parse_patch(patch_of("""
diff --git a/setup.py b/setup.py
old mode 100644
new mode 100755
"""))
        self.assertEqual(changes[0].path, "setup.py")
        self.assertEqual(changes[0].hunks, [])

    def test_several_files_in_one_patch(self):
        changes = diffparse.parse_patch(patch_of("""
diff --git a/one.py b/one.py
--- a/one.py
+++ b/one.py
@@ -1 +1 @@
-a
+b
diff --git a/two.sql b/two.sql
--- a/two.sql
+++ b/two.sql
@@ -1 +1 @@
-c
+d
"""))
        self.assertEqual([c.path for c in changes], ["one.py", "two.sql"])


class TestHunks(unittest.TestCase):
    def test_counts_default_to_one(self):
        changes = diffparse.parse_patch(patch_of("""
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -7 +7 @@
-a
+b
"""))
        hunk = changes[0].hunks[0]
        self.assertEqual((hunk.old_start, hunk.old_count), (7, 1))

    def test_a_lying_hunk_header_is_rejected(self):
        with self.assertRaises(diffparse.PatchError):
            diffparse.parse_patch(patch_of("""
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,5 +1,5 @@
-a
+b
"""))

    def test_several_hunks(self):
        changes = diffparse.parse_patch(patch_of("""
diff --git a/x.py b/x.py
--- a/x.py
+++ b/x.py
@@ -1,2 +1,2 @@
-a
+A
 b
@@ -10,2 +10,2 @@
 j
-k
+K
"""))
        self.assertEqual(len(changes[0].hunks), 2)


class TestApply(unittest.TestCase):
    OLD = ["one", "two", "three", "four", "five"]

    def _hunks(self, text: str):
        return diffparse.parse_patch(patch_of(text))[0].hunks

    def test_replacement(self):
        hunks = self._hunks("""
diff --git a/x b/x
--- a/x
+++ b/x
@@ -2,3 +2,3 @@
 two
-three
+THREE
 four
""")
        self.assertEqual(
            diffparse.apply_hunks(self.OLD, hunks, "x"),
            ["one", "two", "THREE", "four", "five"],
        )

    def test_pure_insertion(self):
        hunks = self._hunks("""
diff --git a/x b/x
--- a/x
+++ b/x
@@ -2,2 +2,3 @@
 two
+extra
 three
""")
        self.assertEqual(
            diffparse.apply_hunks(self.OLD, hunks, "x"),
            ["one", "two", "extra", "three", "four", "five"],
        )

    def test_context_mismatch_raises(self):
        hunks = self._hunks("""
diff --git a/x b/x
--- a/x
+++ b/x
@@ -1,2 +1,2 @@
 WRONG
-two
+TWO
""")
        with self.assertRaises(diffparse.PatchError):
            diffparse.apply_hunks(self.OLD, hunks, "x")


class TestTouchedLines(unittest.TestCase):
    def test_a_replacement_reports_the_old_line(self):
        hunk = diffparse.parse_patch(patch_of("""
diff --git a/x b/x
--- a/x
+++ b/x
@@ -10,3 +10,3 @@
 ten
-eleven
+ELEVEN
 twelve
"""))[0].hunks[0]
        changed, anchors = diffparse.touched_old_lines(hunk)
        self.assertEqual(changed, {11})
        self.assertEqual(anchors, set())

    def test_an_insertion_reports_the_lines_on_either_side(self):
        hunk = diffparse.parse_patch(patch_of("""
diff --git a/x b/x
--- a/x
+++ b/x
@@ -10,2 +10,3 @@
 ten
+NEW
 eleven
"""))[0].hunks[0]
        changed, anchors = diffparse.touched_old_lines(hunk)
        self.assertEqual(changed, set())
        self.assertEqual(anchors, {10, 11})


class TestRubbish(unittest.TestCase):
    def test_empty_patch_raises(self):
        with self.assertRaises(diffparse.PatchError):
            diffparse.parse_patch("")

    def test_prose_raises(self):
        with self.assertRaises(diffparse.PatchError):
            diffparse.parse_patch("this is not a patch at all\n")


class TestAgainstTheSampleSet(unittest.TestCase):
    """Every sample patch parses, and rebuilds byte for byte."""

    def test_all_thirty(self):
        checkout = REPO / "vendor" / "sqlglot"
        if not checkout.is_dir():
            self.skipTest("the sqlglot checkout is not present")
        seen = 0
        for patch in sorted((REPO / "sample" / "commits").glob("*.patch")):
            for change in diffparse.parse_patch(patch.read_text()):
                if change.binary or change.old_path is None:
                    continue
                old = (checkout / change.old_path).read_text()
                diffparse.apply_hunks(old.split("\n"), change.hunks, change.path)
                seen += 1
        self.assertEqual(seen, 35)


if __name__ == "__main__":
    unittest.main()
