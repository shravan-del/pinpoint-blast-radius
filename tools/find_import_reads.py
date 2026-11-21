#!/usr/bin/env python3
"""Which tables are read while sqlglot is being imported?

This matters because sqlglot derives things from whole tables at import:
`new_trie(TIME_MAPPING)`, `INVERSE_TIME_MAPPING`, and
`required_args = {k for k, v in cls.arg_types.items() if v}` in
`__init_subclass__`.  A test can then be affected by a changed entry without
ever looking that key up, because what it reads is the derived structure.

Any table read during import therefore cannot be narrowed by key.  Modules are
imported one at a time and instrumented as we go, so a later module's import
sees the earlier ones' tables exactly as it would normally.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SQLGLOT_ROOT = Path(os.environ.get("PINPOINT_SQLGLOT_ROOT", "/src/sqlglot"))
OUT = Path(os.environ.get("PINPOINT_IMPORT_READS",
                          "/work/eval/maps-work/table_import_reads.json"))


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent / "maps"))
    sys.path.insert(0, str(SQLGLOT_ROOT))
    os.environ["SKIP_INTEGRATION"] = "0"

    import importlib
    import pkgutil

    import tabletrack

    import sqlglot

    tabletrack.install(sqlglot)
    names = [info.name for info in pkgutil.walk_packages(
        sqlglot.__path__, prefix="sqlglot.") if not info.name.endswith("__main__")]
    for name in names:
        try:
            module = importlib.import_module(name)
        except Exception:  # noqa: BLE001
            continue
        try:
            tabletrack.install(module)
        except Exception:  # noqa: BLE001
            pass

    during_import = {table: sorted(keys) for table, keys in tabletrack.READS.items()}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"read_during_import": sorted(during_import)},
                              separators=(",", ":")))
    total = len(tabletrack.TABLES)
    print(f"tables wrapped: {total}", file=sys.stderr)
    print(f"read during import: {len(during_import)} "
          f"({100 * len(during_import) / max(total, 1):.0f}%)", file=sys.stderr)
    for table in sorted(during_import)[:12]:
        print(f"  {table}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
