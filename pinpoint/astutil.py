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


def fingerprint_tree(tree: ast.AST) -> str | None:
    """The fingerprint of an already-parsed tree.  Mutates it: docstrings go."""
    strip_docstrings(tree)
    try:
        dumped = ast.dump(tree, annotate_fields=True, include_attributes=False)
    except (RecursionError, MemoryError):
        return None
    return hashlib.sha256(dumped.encode("utf-8")).hexdigest()


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
    return fingerprint_tree(tree)


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

        # A lambda's body is its own scope.  It does not run when the
        # statement around it runs - building the lambda does not call it -
        # so changing it cannot alter which other lines of that statement
        # execute, and the only tests it can reach are the ones that called
        # it.  Without this, every lambda in a dialect table belongs to the
        # one enormous assignment that defines the table, and editing one of
        # them selects every importer.
        if isinstance(node, ast.Lambda):
            arguments = node.args
            # A default argument IS evaluated when the lambda is built, so a
            # lambda that has one is left alone rather than risk treating a
            # default as deferred when it shares a line with the body.
            if not (arguments.defaults or any(arguments.kw_defaults or ())):
                start = getattr(node.body, "lineno", None)
                end = getattr(node.body, "end_lineno", None)
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


def class_body_lines(tree: ast.AST) -> dict[int, str]:
    """Map a line to the class whose *body* it belongs to, if any.

    A line counts only when the innermost thing containing it is a class body,
    not a method.  `arg_types = {...}` in a class body is in; a line inside one
    of that class's methods is not, because a method body already has ordinary
    line coverage that says who ran it.

    The value is the class's qualified name, so a nested class reads as
    "Snowflake.Tokenizer".
    """
    owner: dict[int, str] = {}

    def visit(node: ast.AST, enclosing: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                qualified = f"{enclosing}.{child.name}" if enclosing else child.name
                end = child.end_lineno
                if child.body and end is not None:
                    for line in range(child.body[0].lineno, end + 1):
                        owner[line] = qualified
                visit(child, qualified)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # Everything inside a function belongs to the function.
                end = child.end_lineno
                if child.body and end is not None:
                    for line in range(child.body[0].lineno, end + 1):
                        owner.pop(line, None)
                visit(child, None)
            else:
                visit(child, enclosing)

    visit(tree, None)
    return owner


def method_body_lines(tree: ast.AST) -> dict[str, list[tuple[int, int]]]:
    """For each class, the line spans of the methods defined directly in it."""
    spans: dict[str, list[tuple[int, int]]] = {}

    def visit(node: ast.AST, enclosing: str | None) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                qualified = f"{enclosing}.{child.name}" if enclosing else child.name
                for inner in child.body:
                    if isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if inner.body and inner.end_lineno is not None:
                            spans.setdefault(qualified, []).append(
                                (inner.body[0].lineno, inner.end_lineno)
                            )
                visit(child, qualified)
            else:
                visit(child, enclosing if isinstance(child, ast.ClassDef) else None)

    visit(tree, None)
    return spans


def _dotted(node: ast.AST) -> str | None:
    """The dotted path of a Name/Attribute chain, or None."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def attribute_reads(tree: ast.AST) -> dict[str, set[int]]:
    """Lines that read an attribute *of* a named class.

    `set(exp.Select.arg_types)` reads Select's data without ever building one,
    and that is the read a class-usage map cannot see.  What is recorded is
    the owner of the attribute, by every dotted suffix of its path, so
    `Snowflake.Tokenizer.KEYWORDS` records "Snowflake.Tokenizer" and
    "Tokenizer" and `exp.Select.arg_types` records "exp.Select" and "Select".

    Only the owner counts, never a bare mention.  `Dialects.TSQL = "tsql"`
    defines an enum member and `"TRIGGER": exp.TriggerProperties` passes the
    class along to something that will build one later - the first changes
    nothing, and the second shows up as an instantiation. Counting bare
    mentions made TSQL and TriggerProperties look like every test in the
    suite.
    """
    found: dict[str, set[int]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute):
            continue
        line = getattr(node, "lineno", None)
        if line is None:
            continue
        owner = _dotted(node.value)
        if owner is None:
            continue
        parts = owner.split(".")
        for start in range(len(parts)):
            found.setdefault(".".join(parts[start:]), set()).add(line)
    return found


def names_and_strings(tree: ast.AST) -> set[str]:
    """Every identifier and string literal in a tree.

    Used as a guard: narrowing a class-body change to "the tests that use this
    class" is only sound while the edit does not add or remove a name or a
    string.  A changed key, a renamed function, a new dialect name can be
    looked up from anywhere by that name, and no amount of class-usage
    evidence bounds it.
    """
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add("n:" + node.id)
        elif isinstance(node, ast.Attribute):
            found.add("a:" + node.attr)
        elif isinstance(node, ast.keyword) and node.arg:
            found.add("k:" + node.arg)
        elif isinstance(node, ast.arg):
            found.add("p:" + node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add("d:" + node.name)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            found.add("s:" + node.value)
        elif isinstance(node, ast.alias):
            found.add("i:" + node.name + ":" + (node.asname or ""))
    return found


def narrowable_class_lines(tree: ast.AST) -> set[int]:
    """Class-body lines whose change cannot escape the class.

    A statement in a class body runs at import, so if it *calls* something the
    call happens for every importer and may do anything - register in a global
    table, mutate shared state. Attributing such a change to "the tests that
    use this class" would then be wrong, so those lines are excluded and keep
    the safe statement-wide answer.

    Two things are narrowable:

      * a statement that evaluates no call at all, which is what a plain table
        of constants is (`arg_types = {"this": True, ...}`);
      * any line inside a `lambda`, because a lambda body does not run at
        import. It runs when something calls it, and to call it you need the
        class.
    """
    narrowable: set[int] = set()
    deferred: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Lambda) and node.body is not None:
            start = getattr(node.body, "lineno", None)
            end = getattr(node.body, "end_lineno", None)
            if start is not None and end is not None:
                deferred.update(range(start, end + 1))

    def has_immediate_call(node: ast.AST) -> bool:
        for child in ast.iter_child_nodes(node):
            # A lambda's body and a nested def's body are not run now.
            if isinstance(child, ast.Lambda):
                continue
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # The decorators and defaults do run now; the body does not.
                for part in [*child.decorator_list, *child.args.defaults]:
                    if isinstance(part, ast.Call) or has_immediate_call(part):
                        return True
                continue
            if isinstance(child, ast.Call):
                return True
            if has_immediate_call(child):
                return True
        return False

    for node in ast.walk(tree):
        if not isinstance(node, _SPAN_NODES):
            continue
        start = getattr(node, "lineno", None)
        end = getattr(node, "end_lineno", None)
        if start is None or end is None:
            continue
        if isinstance(node, ast.Call) or has_immediate_call(node):
            continue
        narrowable.update(range(start, end + 1))

    return narrowable | deferred


def narrowable_def_headers(tree: ast.AST) -> set[int]:
    """Lines of a `def` header whose change only affects callers.

    A default argument is evaluated once, at import, and stored on the
    function object; an annotation under `from __future__ import annotations`
    is never evaluated at all.  Changing either can only be noticed by a call
    that reaches the function body, so the blast radius is the tests that ran
    that body - not every test that imports the module, which is what the
    statement rule has to say about a `def` line.

    Decorator lines are excluded: a decorator runs at import and can do
    anything.  So is a header whose defaults contain a call, for the same
    reason.
    """
    headers: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if not node.body:
            continue
        arguments = node.args
        defaults = [*arguments.defaults, *(arguments.kw_defaults or [])]
        if any(
            value is not None
            and any(isinstance(inner, ast.Call) for inner in ast.walk(value))
            for value in defaults
        ):
            continue
        # From the `def` line to the line before the body starts.  Decorators
        # sit above the `def` line and are deliberately left out.
        headers.update(range(node.lineno, node.body[0].lineno))
    return headers


_INERT_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def module_level_names(tree: ast.AST) -> set[str]:
    """Every name bound at a module's top level."""
    bound: set[str] = set()
    for statement in getattr(tree, "body", []):
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(statement.name)
        elif isinstance(statement, (ast.Import, ast.ImportFrom)):
            for alias in statement.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    bound.add(target.id)
        elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(statement.target, ast.Name):
                bound.add(statement.target.id)
    return bound


def added_statements_are_inert(old_tree: ast.AST, new_tree: ast.AST) -> bool:
    """True when the new module is the old one plus harmless function defs.

    Adding an undecorated `def` at module level binds a name and nothing else:
    the body does not run, and nothing can call it because nothing names it
    yet.  Adding anything else is not harmless - a class can register itself
    through a metaclass or `__init_subclass__`, an import runs another
    module, a decorator runs at import, and a bare statement does whatever it
    says.

    The new name must also be genuinely new, including not shadowing a builtin
    the rest of the module might be using.
    """
    import builtins
    import difflib

    old_body = getattr(old_tree, "body", [])
    new_body = getattr(new_tree, "body", [])
    old_dumps = [ast.dump(node, include_attributes=False) for node in old_body]
    new_dumps = [ast.dump(node, include_attributes=False) for node in new_body]

    taken = module_level_names(old_tree) | set(dir(builtins))
    matcher = difflib.SequenceMatcher(a=old_dumps, b=new_dumps, autojunk=False)
    for tag, _, _, start, end in matcher.get_opcodes():
        if tag == "equal":
            continue
        if tag != "insert":
            return False
        for node in new_body[start:end]:
            if not isinstance(node, _INERT_DEFS):
                return False
            if node.decorator_list:
                return False
            if node.name in taken:
                return False
    return True
