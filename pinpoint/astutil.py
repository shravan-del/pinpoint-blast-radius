"""Python-source analysis shared by the map builder and the selector.

Both sides must agree exactly, so the fingerprint and the statement model live
in one file.  Standard library only: the selector imports this at scoring time.
"""

from __future__ import annotations

import ast
import hashlib

# Nodes that own a span of lines.  ast.stmt covers ordinary statements;
# ExceptHandler is not a statement but coverage reports its `except ...:` line.
_SPAN_NODES = (ast.stmt, ast.ExceptHandler)


def strip_docstrings(tree: ast.AST) -> None:
    """Drop every docstring, in place.

    A docstring is a bare string expression first in a module, class or
    function body.  No numbered test reads one (see NOTES.md), so two files
    that differ only in their docstrings behave identically.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = node.body
        if not body:
            continue
        first = body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            # Keep the body non-empty so the tree stays valid.
            body[0] = ast.Pass()


def fingerprint(source: str) -> str | None:
    """A hash of what the file *means*, or None when it does not parse.

    Line numbers, column offsets, comments, blank lines and docstrings are all
    excluded, so two sources with the same fingerprint cannot behave
    differently.  `ast.dump` with `include_attributes=False` is what drops the
    positions; comments and blank lines never reach the tree at all.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return None
    strip_docstrings(tree)
    try:
        dumped = ast.dump(tree, annotate_fields=True, include_attributes=False)
    except (RecursionError, MemoryError):
        return None
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


def statement_owners(tree: ast.AST) -> dict[int, int]:
    """Map every line of a file to the first line of its innermost statement.

    coverage.py reports a multi-line statement at its first line only, so a
    change in the middle of a multi-line dict, call or table (the dialect
    KEYWORDS / FUNCTIONS / TRANSFORMS tables are exactly this) has to be
    attributed to that first line before it can be looked up in the line map.

    Parents are written first and children overwrite them, so the innermost
    statement wins.  Decorator expressions get their own entry because
    coverage reports a decorator line separately from the `def` line it
    belongs to.
    """
    owners: dict[int, int] = {}

    def claim(start: int, end: int, anchor: int) -> None:
        for line in range(start, end + 1):
            owners[line] = anchor

    def visit(node: ast.AST) -> None:
        if isinstance(node, _SPAN_NODES):
            start = getattr(node, "lineno", None)
            end = getattr(node, "end_lineno", None)
            if start is not None and end is not None:
                claim(start, end, start)

        # A decorated def: the decorator lines run on their own.
        for decorator in getattr(node, "decorator_list", ()) or ():
            start = getattr(decorator, "lineno", None)
            end = getattr(decorator, "end_lineno", None)
            if start is not None and end is not None:
                claim(start, end, start)

        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return owners


def block_ranges(tree: ast.AST) -> dict[int, tuple[int, int]]:
    """Map every line to the *body* of its innermost def or class.

    Used for a pure insertion: the new lines have no old counterpart, so the
    blast radius is every test that ran anything in the block the insertion
    landed in.  Lines outside any def or class map to nothing and the caller
    falls back to the whole module.

    The value is the span of the block's **body**, not of the whole block.
    Including the `def` line would make every function's block look as though
    every importer had run it - a `def` runs at import - and an insertion
    anywhere in any function body would then select the whole module.  A
    change to the signature itself is not lost: those lines belong to the
    `def` statement, and the caller looks that up as well.
    """
    blocks: dict[int, tuple[int, int]] = {}

    def visit(node: ast.AST) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = node.lineno
            end = node.end_lineno
            if end is not None and node.body:
                # Decorators belong to the block they decorate.
                for decorator in node.decorator_list:
                    start = min(start, decorator.lineno)
                interior = (node.body[0].lineno, end)
                for line in range(start, end + 1):
                    blocks[line] = interior
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return blocks
