#!/usr/bin/env python3
"""Upper bound for the value-level infection check, before building it.

T2 can only fire when a hunk changes exactly one simple expression on one
line: an operator swap, a constant change, a comparison flip.  Where it fires
and the recorded operand values say old and new agree, the test is not
infected and can be dropped.

The bound assumes the recording is perfect and always decides - every
non-failing covering test gets dropped - so the commit costs exactly what the
oracle costs.  Two bounds are reported, because the operands decide whether a
value can be recorded at all: one counting only the primitives the plan
allows, and one that also counts enum members, which is what sqlglot's
comparisons are actually made of.
"""
from __future__ import annotations

import argparse
import ast
import json
import math
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))
import selector  # noqa: E402
from pinpoint import choose, diffparse  # noqa: E402
from pinpoint.mapstore import MapStore, bits_to_numbers  # noqa: E402

WORKER = 2_000


def pack_cost(numbers, durations):
    workers = selector.pack(list(numbers), durations)
    if not workers:
        return 0
    return max(sum(durations[n] for n in w) for w in workers) + WORKER * len(workers)


class Normalise(ast.NodeTransformer):
    """Blank out constant values and operators, keeping the shape."""

    def visit_Constant(self, node):          # noqa: N802
        return ast.copy_location(ast.Constant(value="?"), node)


def shape(tree) -> str:
    cleaned = Normalise().visit(ast.parse(ast.unparse(tree)))
    dumped = ast.dump(cleaned, include_attributes=False)
    for operator in ("Eq", "NotEq", "Lt", "LtE", "Gt", "GtE", "Is", "IsNot",
                     "In", "NotIn", "Add", "Sub", "Mult", "Div", "FloorDiv",
                     "Mod", "Pow", "And", "Or", "BitAnd", "BitOr", "BitXor",
                     "LShift", "RShift", "Not", "USub", "UAdd"):
        dumped = dumped.replace(f"{operator}()", "OP()")
    return dumped


def constants_of(tree) -> list:
    return sorted(
        (type(n.value).__name__, repr(n.value))
        for n in ast.walk(tree) if isinstance(n, ast.Constant)
    )


def simple_expression_edit(old_text: str, new_text: str):
    """How the files differ: not at all, only in operators, or in constants.

    An operator swap can be value-preserving on every input a test ever
    supplies - `is` against `==` on enum members always agrees - so T2 can
    drop the covering tests.  A changed constant usually differs on every
    input, so T2 decides "infected" and drops nothing.  Lumping them together
    flatters the bound.
    """
    try:
        old_tree = ast.parse(old_text)
        new_tree = ast.parse(new_text)
    except SyntaxError:
        return None
    try:
        if shape(old_tree) != shape(new_tree):
            return None
        if (ast.dump(old_tree, include_attributes=False)
                == ast.dump(new_tree, include_attributes=False)):
            return None
    except Exception:  # noqa: BLE001
        return None
    return "operator" if constants_of(old_tree) == constants_of(new_tree) \
        else "constant"


def main(argv):
    parser = argparse.ArgumentParser()
    parser.add_argument("data_dirs", nargs="+", type=Path)
    parser.add_argument("--maps", type=Path, default=REPO / "maps" / "maps.zip")
    options = parser.parse_args(argv)
    store = MapStore(options.maps)
    everything = store.all_tests()

    grand = {}
    for data_dir in options.data_dirs:
        tests = json.loads((data_dir / "tests.json").read_text())["tests"]
        answers = json.loads((data_dir / "answers.json").read_text())["commits"]
        durations = {t["number"]: math.floor(t["seconds"] * 1000 + 0.5) for t in tests}
        now_total = bound_total = oracle_total = operator_bound = 0
        fires = operator_fires = 0
        for commit_id in sorted(answers):
            text = (data_dir / "commits" / f"{commit_id}.patch").read_text(
                "utf-8", errors="replace")
            try:
                bits = choose.select(text, store)
                chosen = (list(durations) if bits == everything
                          else bits_to_numbers(bits))
            except Exception:  # noqa: BLE001
                chosen = list(durations)
            failing = answers[commit_id]["failing"]
            now = pack_cost(chosen, durations)
            oracle = pack_cost(failing, durations)
            now_total += now
            oracle_total += oracle

            kinds = set()
            eligible = False
            try:
                changes = diffparse.parse_patch(text)
                if all(c.status == diffparse.MODIFIED and not c.binary
                       and c.path.endswith(".py") for c in changes):
                    eligible = True
                    for change in changes:
                        old_text = store.source(change.path)
                        if old_text is None:
                            eligible = False
                            break
                        new_text = "\n".join(diffparse.apply_hunks(
                            old_text.split("\n"), change.hunks, change.path))
                        verdict = simple_expression_edit(old_text, new_text)
                        if verdict is None:
                            eligible = False
                            break
                        kinds.add(verdict)
            except Exception:  # noqa: BLE001
                eligible = False
            if eligible:
                fires += 1
                bound_total += oracle
                if kinds == {"operator"}:
                    operator_fires += 1
                    operator_bound += oracle
                else:
                    operator_bound += now
            else:
                bound_total += now
                operator_bound += now
        n = len(answers)
        grand[data_dir.name] = (n, now_total, bound_total, oracle_total, fires,
                                operator_bound, operator_fires)

    print(f"{'set':10s} {'n':>4s} {'now/c':>7s} {'all-edits/c':>11s} "
          f"{'op-only/c':>10s} {'oracle/c':>9s}  fires(all / operator-only)")
    tn = tnow = tall = top = torc = 0
    for name, (n, now, bound, oracle, fires, opbound, opfires) in grand.items():
        print(f"{name:10s} {n:4d} {now/1000/n:7.3f} {bound/1000/n:11.3f} "
              f"{opbound/1000/n:10.3f} {oracle/1000/n:9.3f}   {fires:3d}/{n}  "
              f"{opfires:3d}/{n}")
        tn += n
        tnow += now
        tall += bound
        top += opbound
        torc += oracle
    print(f"{'TOTAL':10s} {tn:4d} {tnow/1000/tn:7.3f} {tall/1000/tn:11.3f} "
          f"{top/1000/tn:10.3f} {torc/1000/tn:9.3f}")
    print(f"\nsaved: all-edits {(tnow-tall)/1000:+.1f} s "
          f"({(tnow-tall)/1000/tn:+.3f}/c), "
          f"operator-only {(tnow-top)/1000:+.1f} s ({(tnow-top)/1000/tn:+.3f}/c)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
