"""From one patch to the tests it could break.

Selections are integer bit masks over test numbers: bit *i* is set when test
*i* is selected.  Unions are then a single ``|`` and "everything" is a single
comparison, which keeps the per-commit work in microseconds.

Every rule here is evidence from the pinned commit - which tests ran which
line, which tests imported which module, which tests read which file - and
never a rule about a kind of patch.  Where the evidence runs out, the answer
is every test.
"""

from __future__ import annotations

import ast

from . import astutil
from .diffparse import ADDED, DELETED, apply_hunks, parse_patch, touched_old_lines

# There is no conftest.py anywhere at the pinned commit.  One appearing, or an
# existing one changing, rewrites how pytest collects and runs everything.
CONFTEST = "conftest.py"


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _watchers(path: str, store) -> int:
    """Tests that enumerate the directory a file sits in.

    A file that did not exist at the pinned commit can only be noticed by a
    test that lists its directory, which is what the directory-watcher map
    records.  tests/test_typing.py walking sqlglot/typing/ with
    pkgutil.iter_modules is the live example, and it shows up there as exactly
    one test.

    Only the file's own directory counts, not every directory above it.  A
    recursive walk lists each directory it descends into, so it appears in the
    map once per directory and is not missed; whereas charging a file for every
    ancestor would mean anything added anywhere under tests/ inherited the
    1620 tests that list tests/ during pytest's own collection.
    """
    directory = path.rsplit("/", 1)[0] if "/" in path else ""
    group = store.meta.get("directory_watchers", {}).get(directory)
    return store.group(group) if group is not None else 0


def _everything_touching(path: str, store) -> int:
    return (
        store.tests_in_module(path)
        | store.tests_importing(path)
        | store.tests_opening(path)
        | store.tests_defined_in(path)
    )


def _for_removal(path: str, store, everything: int) -> int:
    if not store.knows(path) and path not in store.repo_paths:
        return everything
    return _everything_touching(path, store) | _watchers(path, store)


def _module_name(path: str) -> str | None:
    """The dotted module a repo-relative .py path is imported as."""
    if not path.endswith(".py"):
        return None
    parts = path[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts) if parts else None


def _for_addition(path: str, store, everything: int) -> int:
    if store.knows(path) or path in store.repo_paths:
        # The patch adds a file our snapshot already has, so one of the two is
        # wrong about the pinned commit.  Do not guess.
        return everything

    found = _watchers(path, store)

    # A new file is not always inert.  If tests already tried to import this
    # module and the import failed, adding the file makes it start succeeding.
    module = _module_name(path)
    if module:
        absent = store.meta.get("absent_modules", {})
        group = absent.get(module)
        if group is not None:
            found |= store.group(group)
    return found


def _for_modification(change, store, everything: int) -> int:
    path = change.path
    if not store.knows(path):
        if path in store.repo_paths:
            # The file is there at the pinned commit and no test ran, imported
            # or read it.  Nothing can see the change.
            return 0
        return everything

    opened_as_data = store.tests_opening(path)
    if not path.endswith(".py"):
        return opened_as_data

    old_text = store.source(path)
    if old_text is None:
        return everything

    new_lines = apply_hunks(old_text.split("\n"), change.hunks, path)
    fingerprint = astutil.fingerprint("\n".join(new_lines))

    if fingerprint is None:
        # The new file does not parse.  Importing it raises, so every test that
        # imports it errors, as does every test defined in it.
        return (
            store.tests_in_module(path)
            | store.tests_importing(path)
            | store.tests_defined_in(path)
            | opened_as_data
        )

    if fingerprint == store.ast_fingerprint(path):
        # Same syntax tree once positions and docstrings are dropped, so the
        # change is comments, blank lines, layout or a docstring.  Nothing can
        # behave differently.  A test that reads the file as *data* still can.
        return opened_as_data

    # Narrowing a class-body change to the tests that use that class is only
    # sound while the edit does not add or remove a name or a string.  A
    # changed key, a renamed helper or a new dialect name can be looked up by
    # that name from anywhere, and no amount of class-usage evidence bounds it.
    # Answering it costs a second parse of the whole file, so it is only asked
    # when a changed line really does sit in a class body.
    guard: list[bool] = []

    def may_narrow() -> bool:
        if not guard:
            guard.append(store.has_class_map and _same_names(old_text, new_lines))
        return guard[0]

    found = opened_as_data
    for hunk in change.hunks:
        changed, insertions = touched_old_lines(hunk)
        for line in changed:
            narrowed = None
            if store.class_at(path, line) is not None and may_narrow():
                narrowed = _class_body_tests(path, line, store)
            found |= store.tests_for_line(path, line) if narrowed is None else narrowed
        for line in insertions:
            # The gap sits inside some statement - a multi-line signature, a
            # table literal - and inserting into it changes that statement.
            found |= store.tests_for_line(path, line)
            if store.has_block(path, line):
                # New lines inside a def or class body: anything that ran it.
                found |= store.tests_for_block(path, line)
            else:
                # New lines at module level run on import.
                found |= store.tests_in_module(path)
    return found



def _same_names(old_text: str, new_lines: list[str]) -> bool:
    """True when the edit changed no identifier and no string literal."""
    try:
        before = astutil.names_and_strings(ast.parse(old_text))
        after = astutil.names_and_strings(ast.parse("\n".join(new_lines)))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return False
    return before == after


def _class_body_tests(path: str, line: int, store) -> int | None:
    """Tests a change to this class-body line could reach, or None to fall back.

    A statement in a class body runs at import, so every importer executes it
    and line coverage cannot tell them apart.  What does tell them apart is
    whether the test ever used the class.  Three ways a test can be reached by
    the change, and all three are measured:

      * it built an instance of a class with this one in its MRO;
      * it ran a method defined in this class, which covers a classmethod
        reached without an instance;
      * it ran a line that mentions the class by name, which covers reading
        the class's data without building one, as `set(exp.Select.arg_types)`
        does.

    None means the evidence does not apply and the caller keeps the safe
    statement-wide answer.
    """
    qualified = store.class_at(path, line)
    if qualified is None:
        return None
    module = path[:-3].replace("/", ".")
    if module.endswith(".__init__"):
        module = module[: -len(".__init__")]
    label = f"{module}:{qualified}"
    if not store.class_is_trackable(label):
        return None
    found = store.tests_using_class(label)
    found |= store.tests_running_methods_of(path, qualified)
    # Keyed on the class's own qualified name, so a nested class is looked up
    # as "Snowflake.Tokenizer" rather than as every Tokenizer in the project.
    found |= store.tests_naming(qualified)
    return found


def _for_change(change, store, everything: int) -> int:
    if _basename(change.path) == CONFTEST or _basename(change.old_path or "") == CONFTEST:
        return everything

    if change.renamed:
        return (
            _for_removal(change.old_path, store, everything)
            | _for_addition(change.new_path, store, everything)
        )
    if change.status == DELETED:
        return _for_removal(change.old_path, store, everything)
    if change.status == ADDED:
        return _for_addition(change.new_path, store, everything)
    if change.binary:
        # We cannot read the new bytes, so assume the worst about this file.
        if not store.knows(change.path):
            return everything
        return _everything_touching(change.path, store)
    return _for_modification(change, store, everything)


def select(patch_text: str, store) -> int:
    """The tests one commit could break, as a bit mask.  May raise."""
    everything = store.all_tests()
    found = 0
    for change in parse_patch(patch_text):
        found |= _for_change(change, store, everything)
        if found == everything:
            return everything
    return found
