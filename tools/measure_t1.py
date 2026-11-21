#!/usr/bin/env python3
"""Upper bound for Trivial Compiler Equivalence, before building it in.

For every labeled commit: do all its changed .py files compile to identical
code objects, and does it touch nothing else?  If so the commit cannot change
behaviour and should select nothing.  Reports how often that fires, how often
it fires where the syntax-tree rule does not, and - the thing that matters -
whether every firing commit really has no failing tests.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
from pinpoint import astutil, diffparse  # noqa: E402
from pinpoint.mapstore import MapStore  # noqa: E402


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("data_dirs", nargs="+", type=Path)
    parser.add_argument("--maps", type=Path, default=REPO / "maps" / "maps.zip")
    options = parser.parse_args(argv)
    store = MapStore(options.maps)

    total = 0
    code_same = 0
    ast_same = 0
    code_only = 0
    unsafe: list[str] = []
    code_only_commits: list[str] = []

    for data_dir in options.data_dirs:
        answers = json.loads((data_dir / "answers.json").read_text())["commits"]
        for commit_id in sorted(answers):
            total += 1
            text = (data_dir / "commits" / f"{commit_id}.patch").read_text(
                "utf-8", errors="replace")
            try:
                changes = diffparse.parse_patch(text)
            except Exception:  # noqa: BLE001
                continue
            all_code_same = True
            all_ast_same = True
            for change in changes:
                path = change.path
                if (change.binary or change.status != diffparse.MODIFIED
                        or not path.endswith(".py")):
                    all_code_same = all_ast_same = False
                    break
                old_text = store.source(path)
                if old_text is None:
                    all_code_same = all_ast_same = False
                    break
                try:
                    new_text = "\n".join(diffparse.apply_hunks(
                        old_text.split("\n"), change.hunks, path))
                except Exception:  # noqa: BLE001
                    all_code_same = all_ast_same = False
                    break
                if not astutil.compiles_the_same(old_text, new_text):
                    all_code_same = False
                if astutil.fingerprint(new_text) != store.ast_fingerprint(path):
                    all_ast_same = False
                if not all_code_same and not all_ast_same:
                    break
            if all_code_same:
                code_same += 1
                if not all_ast_same:
                    code_only += 1
                    code_only_commits.append(f"{data_dir.name}/{commit_id}")
                if answers[commit_id]["failing"]:
                    unsafe.append(f"{data_dir.name}/{commit_id} "
                                  f"({len(answers[commit_id]['failing'])} failing)")
            if all_ast_same:
                ast_same += 1

    print(f"labeled commits:                        {total}")
    print(f"all changed files compile identically:  {code_same}")
    print(f"  of those, the AST rule already fires: {code_same - code_only}")
    print(f"  T1 fires where the AST rule does NOT: {code_only}")
    for name in code_only_commits[:20]:
        print(f"      {name}")
    print(f"syntax-tree rule fires on:              {ast_same}")
    if unsafe:
        print(f"\nUNSAFE - compiles identically but has failing tests: {len(unsafe)}")
        for name in unsafe[:20]:
            print(f"   {name}")
    else:
        print("\nevery commit that compiles identically has failing == []")
    return 1 if unsafe else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
