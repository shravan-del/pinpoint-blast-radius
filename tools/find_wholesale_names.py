#!/usr/bin/env python3
"""Attribute names that some code reads as a whole table, found statically.

The runtime recorder sees a wholesale read only when it happens after the
table has been wrapped.  It cannot see one that happens *while the defining
module is still executing* - and sqlglot has exactly that:
`required_args = {k for k, v in cls.arg_types.items() if v}` runs in
`__init_subclass__` as each class is created.  A change to one entry of
arg_types changes required_args, and a test can then fail without ever
looking that key up.

So any name read wholesale anywhere in the repo is barred from key-level
narrowing, whether or not the recorder happened to see it.
"""
from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path

SQLGLOT_ROOT = Path(os.environ.get("PINPOINT_SQLGLOT_ROOT", "/src/sqlglot"))
OUT = Path(os.environ.get("PINPOINT_WHOLESALE",
                          "/work/eval/maps-work/table_wholesale_names.json"))

# A copy keeps the keys, so a change stays reachable through its key.  These
# are the ones that can produce something keyed differently.
WHOLE_METHODS = {"items", "keys", "values", "popitem", "clear"}
WHOLE_CALLS = {"list", "set", "sorted", "tuple", "frozenset",
               "reversed", "iter", "max", "min", "sum", "any", "all"}


def _name_of(node: ast.AST) -> str | None:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return None


DEFINITION_TIME_FUNCTIONS = {"__init_subclass__", "__new__", "__init__"}


def definition_time_nodes(tree: ast.AST):
    """Nodes evaluated when the module and its classes are created.

    A wholesale read inside an ordinary method happens while a test runs, and
    the recorder sees it and marks that test.  One at module level, in a class
    body, or in __init_subclass__ happens before any test and before the
    recorder is attached, so only a static scan can find it.
    """
    out = []

    def walk(node, inside_function):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                definition_time = child.name in DEFINITION_TIME_FUNCTIONS
                for part in [*child.decorator_list, *child.args.defaults]:
                    out.extend(ast.walk(part))
                walk(child, not definition_time)
                continue
            if isinstance(child, ast.Lambda):
                continue          # the body runs when something calls it
            if not inside_function:
                out.append(child)
            walk(child, inside_function)

    walk(tree, False)
    return out


def main() -> int:
    found: set[str] = set()
    for path in sorted(SQLGLOT_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text("utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in definition_time_nodes(tree):
            # X.items() / X.keys() / X.values() / X.copy() ...
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in WHOLE_METHODS:
                    name = _name_of(node.func.value)
                    if name:
                        found.add(name)
                        continue
            # dict(X), set(X), sorted(X), len(X) ...
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in WHOLE_CALLS:
                    for argument in node.args:
                        name = _name_of(argument)
                        if name:
                            found.add(name)
            # {**X} and dict(X) copy the entries under the same keys, so a
            # change to one entry is still reachable through that key.  They
            # are how dialect inheritance works and are NOT barred.
            # for k in X:   /   [... for k in X]
            if isinstance(node, (ast.For, ast.AsyncFor)):
                name = _name_of(node.iter)
                if name:
                    found.add(name)
            if isinstance(node, ast.comprehension):
                name = _name_of(node.iter)
                if name:
                    found.add(name)
            # f(*X)
            if isinstance(node, ast.Starred):
                name = _name_of(node.value)
                if name:
                    found.add(name)
            # Handed to a function at definition time: `new_trie(TIME_MAPPING)`
            # derives a differently-keyed structure inside new_trie, where the
            # table's own name never appears, so passing it is barred too.
            if isinstance(node, ast.Call):
                for argument in [*node.args, *(k.value for k in node.keywords)]:
                    name = _name_of(argument)
                    if name:
                        found.add(name)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"wholesale_names": sorted(found)},
                              separators=(",", ":")))
    print(f"names read wholesale somewhere: {len(found)}", file=sys.stderr)
    for name in ("arg_types", "TRANSFORMS", "FUNCTIONS", "KEYWORDS",
                 "TYPE_MAPPING", "EXPRESSION_METADATA", "TIME_MAPPING",
                 "PROPERTIES_LOCATION", "NO_PAREN_FUNCTIONS"):
        print(f"  {name:22s} {'BARRED' if name in found else 'narrowable'}",
              file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
