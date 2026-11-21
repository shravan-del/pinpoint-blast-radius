"""Record which KEYS of sqlglot's big tables each test actually reads.

Line coverage cannot separate one entry of a dialect table from another: the
table is built once, at import, by every importer.  What separates them is
which key a test looks up.  The consumers are ordinary Python lookups -
`self.expression_metadata.get(expr.__class__)`, `self.TRANSFORMS.get(...)`,
`self.FUNCTIONS.get(name)` - so a dict subclass sees them.

Anything that could read more than one entry, and anything we cannot
attribute to a single key, records the whole table instead.  Reading too much
costs seconds; reading too little costs 300.

Build-time only.  Never shipped.
"""

from __future__ import annotations

ALL_KEYS = "*"

READS: dict[str, set] = {}
TABLES: dict[str, dict] = {}

# Written once, at exit, as a compressed blob.  One line per read would mean
# nearly ten thousand file writes for a single test.
DUMP_PATH = None

# Reads that happen while a module is being imported do not count.  A dialect
# builds its table with `{**PARENT, ...}`, which iterates the parent, and
# counting that would mark the parent wholly read by every importer and
# destroy the whole point.  The entries the spread copies are the *same
# objects*, so the origin map links a child entry back to its parent instead.
IMPORTING = 0


def _key_name(key) -> str:
    """A stable text form of a key, matching what the AST can see."""
    if isinstance(key, bool):
        return "n:" + repr(key)
    if isinstance(key, str):
        return "s:" + key
    if isinstance(key, type):
        return "t:" + getattr(key, "__qualname__", getattr(key, "__name__", "?"))
    if isinstance(key, (int, float)):
        return "n:" + repr(key)
    name = getattr(key, "name", None)
    if isinstance(name, str):
        return "e:" + name
    return "r:" + repr(key)[:80]


def _note(table_id: str, key) -> None:
    if IMPORTING:
        return
    bucket = READS.get(table_id)
    if bucket is None:
        bucket = READS[table_id] = set()
    if ALL_KEYS in bucket:
        return
    try:
        name = _key_name(key)
    except Exception:  # noqa: BLE001 - unattributable, so assume everything
        name = ALL_KEYS
    if name in bucket:
        return
    bucket.add(name)


def _note_all(table_id: str) -> None:
    if IMPORTING:
        return
    if READS.get(table_id) == {ALL_KEYS}:
        return
    READS[table_id] = {ALL_KEYS}


class Tracked(dict):
    """A dict that remembers which keys were looked up.

    Every operation that can see more than one entry - iterating, items(),
    copying, comparing, pickling - records the whole table, because afterwards
    we cannot say which entry mattered.
    """

    __slots__ = ("_pinpoint_id",)

    def __init__(self, contents, table_id):
        super().__init__(contents)
        self._pinpoint_id = table_id

    def __getitem__(self, key):
        _note(self._pinpoint_id, key)
        return super().__getitem__(key)

    def get(self, key, default=None):
        _note(self._pinpoint_id, key)
        return super().get(key, default)

    def __contains__(self, key):
        _note(self._pinpoint_id, key)
        return super().__contains__(key)

    def pop(self, key, *rest):
        _note(self._pinpoint_id, key)
        return super().pop(key, *rest)

    def setdefault(self, key, default=None):
        _note(self._pinpoint_id, key)
        return super().setdefault(key, default)

    def __iter__(self):
        _note_all(self._pinpoint_id)
        return super().__iter__()

    def keys(self):
        _note_all(self._pinpoint_id)
        return super().keys()

    def values(self):
        _note_all(self._pinpoint_id)
        return super().values()

    def items(self):
        _note_all(self._pinpoint_id)
        return super().items()

    def copy(self):
        _note_all(self._pinpoint_id)
        return dict(self)

    def __or__(self, other):
        _note_all(self._pinpoint_id)
        return dict(self) | other

    def __ror__(self, other):
        _note_all(self._pinpoint_id)
        return other | dict(self)

    def __eq__(self, other):
        _note_all(self._pinpoint_id)
        return super().__eq__(other)

    def __ne__(self, other):
        _note_all(self._pinpoint_id)
        return super().__ne__(other)

    __hash__ = None

    def __reduce__(self):
        # Pickled to a spawn worker: it arrives as a plain dict and we cannot
        # see what the worker reads, so the whole table counts.
        _note_all(self._pinpoint_id)
        return (dict, (dict(self),))

    def __repr__(self):
        _note_all(self._pinpoint_id)
        return super().__repr__()


def _is_table(attribute: str, value) -> bool:
    """A real lookup table, not Python's own machinery.

    `__builtins__` is a dict on every module and replacing it with a subclass
    is both meaningless and risky, and every module shares it, so it swamps
    the records.
    """
    if attribute.startswith("__") and attribute.endswith("__"):
        return False
    return isinstance(value, dict) and not isinstance(value, Tracked) and len(value) >= 2


def _wrap(container, attribute, value, table_id) -> bool:
    try:
        tracked = Tracked(value, table_id)
        setattr(container, attribute, tracked)
    except Exception:  # noqa: BLE001
        return False
    TABLES[table_id] = tracked
    return True


def _install_on_class(cls, module_name: str, seen: set) -> None:
    if id(cls) in seen:
        return
    seen.add(id(cls))
    for attribute, value in list(vars(cls).items()):
        if _is_table(attribute, value):
            _wrap(cls, attribute, value, f"{module_name}:{cls.__qualname__}.{attribute}")
        elif isinstance(value, type):
            _install_on_class(value, module_name, seen)


def install(module) -> None:
    """Replace this module's table-shaped dicts, and its classes', with Tracked."""
    name = getattr(module, "__name__", "")
    if not name.startswith("sqlglot"):
        return
    seen: set = set()
    for attribute, value in list(vars(module).items()):
        if _is_table(attribute, value):
            _wrap(module, attribute, value, f"{name}:{attribute}")
        elif isinstance(value, type) and getattr(value, "__module__", None) == name:
            _install_on_class(value, name, seen)


def dump() -> None:
    """Write what this process read.  A test with no dump counts as reading
    everything, so losing this file costs seconds, never correctness."""
    if not DUMP_PATH:
        return
    import gzip
    import json

    try:
        with gzip.open(DUMP_PATH, "wt", encoding="utf-8") as handle:
            json.dump({table: sorted(keys) for table, keys in READS.items()}, handle)
    except Exception:  # noqa: BLE001
        pass


def arm(path: str) -> None:
    import atexit

    global DUMP_PATH
    DUMP_PATH = path
    atexit.register(dump)
