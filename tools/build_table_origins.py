#!/usr/bin/env python3
"""Which tables share an entry with which other tables.

A dialect builds its table with `{**PARENT, ...}`, so the entries it did not
override are the *same objects* as the parent's.  A test that looks up key K
on the child is therefore reading the parent's entry, and changing the parent
has to select it.

Grouping by `id(value)` after everything is imported finds those links without
having to understand the inheritance by hand.

Build-time tool.  Writes a map from (table, key) to every (table, key) that
holds the identical object.
"""

from __future__ import annotations

import collections
import json
import os
import sys
from pathlib import Path

SQLGLOT_ROOT = Path(os.environ.get("PINPOINT_SQLGLOT_ROOT", "/src/sqlglot"))
OUT = Path(os.environ.get("PINPOINT_ORIGINS", "/work/eval/maps-work/table_origins.json"))


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "maps"))
    sys.path.insert(0, str(SQLGLOT_ROOT))
    os.environ["SKIP_INTEGRATION"] = "0"

    import tabletrack

    # Import every sqlglot module, then instrument: the spreads have already
    # happened, so each table holds the objects it will serve at run time.
    import importlib
    import pkgutil

    import sqlglot

    modules = [sqlglot]
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name.endswith("__main__"):
            continue
        try:
            modules.append(importlib.import_module(info.name))
        except Exception:  # noqa: BLE001
            continue
    for module in modules:
        try:
            tabletrack.install(module)
        except Exception:  # noqa: BLE001
            continue

    # Which keys each table holds.  Identity grouping is deliberately NOT
    # used: the values are often True, None or an interned string, which every
    # other table shares, so identity would link everything to everything.
    # Scoping by the key name instead is a superset of the truth and stays
    # specific, because a key like t:Div appears in few tables.
    keys_of: dict[str, list[str]] = {}
    tables_with_key: dict[str, list[str]] = collections.defaultdict(list)
    for table_id, table in tabletrack.TABLES.items():
        names = []
        for key in dict.keys(table):            # no tracking, no recursion
            try:
                name = tabletrack._key_name(key)
            except Exception:  # noqa: BLE001
                continue
            names.append(name)
            tables_with_key[name].append(table_id)
        keys_of[table_id] = sorted(set(names))

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "keys_of_table": keys_of,
        "tables_with_key": {k: sorted(set(v)) for k, v in tables_with_key.items()},
    }, separators=(",", ":")))
    print(f"tables: {len(tabletrack.TABLES)}", file=sys.stderr)
    print(f"distinct keys across all tables: {len(tables_with_key)}", file=sys.stderr)
    biggest = sorted(((len(v), k) for k, v in tables_with_key.items()),
                     reverse=True)[:5]
    for size, key in biggest:
        print(f"  key {key} appears in {size} tables", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
