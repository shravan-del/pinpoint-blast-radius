"""Reader for the static maps built from the pinned commit.

Everything is in one zip so the selector opens one file and then reads only
the few entries a patch actually mentions.  Nothing is decoded eagerly: a
commit that touches one small file must not pay for the whole index.
"""

from __future__ import annotations

import bisect
import json
import zipfile
from pathlib import Path

EMPTY = -1


class MapStore:
    def __init__(self, archive_path: str | Path) -> None:
        self._zip = zipfile.ZipFile(archive_path)
        self._meta: dict | None = None
        self._entries: dict[str, dict | None] = {}
        self._groups: bytes | None = None
        self._repo_paths: frozenset[str] | None = None
        self._names = set(self._zip.namelist())

    # -- lazy pieces -------------------------------------------------------

    @property
    def meta(self) -> dict:
        """The small header: test count, bitset width, packages, watchers."""
        if self._meta is None:
            self._meta = json.loads(self._zip.read("meta.json"))
        return self._meta

    @property
    def test_count(self) -> int:
        return self.meta["test_count"]

    @property
    def bitset_width(self) -> int:
        return self.meta["bitset_width"]

    @property
    def repo_paths(self) -> frozenset[str]:
        """Every file that exists at the pinned commit.

        Lets us tell "a file nothing reads" from "a file we have never heard
        of", which are very different answers.
        """
        if self._repo_paths is None:
            self._repo_paths = frozenset(
                self._zip.read("repo_paths.txt").decode("utf-8").split("\n")
            )
        return self._repo_paths

    def entry(self, path: str) -> dict | None:
        """The map for one repo file, or None when we have no record of it."""
        if path in self._entries:
            return self._entries[path]
        name = "f/" + path + ".json"
        value = json.loads(self._zip.read(name)) if name in self._names else None
        self._entries[path] = value
        return value

    def source(self, path: str) -> str | None:
        """The pinned text of a .py file, or None when it is not bundled."""
        name = "src/" + path
        if name not in self._names:
            return None
        return self._zip.read(name).decode("utf-8")

    def group(self, group_id: int) -> int:
        """One test set, as an integer whose bit i is set when test i is in it."""
        if group_id is None or group_id < 0:
            return 0
        if self._groups is None:
            self._groups = self._zip.read("groups.bin")
        width = self.bitset_width
        start = group_id * width
        return int.from_bytes(self._groups[start:start + width], "little")

    # -- lookups -----------------------------------------------------------

    @staticmethod
    def _run_value(runs: list[list[int]], line: int) -> int:
        """The run-length encoded value for a line."""
        if not runs:
            return EMPTY
        position = bisect.bisect_right(runs, [line, 1 << 62]) - 1
        if position < 0:
            return EMPTY
        return runs[position][1]

    def tests_for_line(self, path: str, line: int) -> int:
        """Tests that ran the statement containing this line of the old file."""
        entry = self.entry(path)
        if not entry:
            return 0
        return self.group(self._run_value(entry.get("runs", []), line))

    def tests_for_block(self, path: str, line: int) -> int:
        """Tests that ran anything in the innermost def or class around a line."""
        entry = self.entry(path)
        if not entry:
            return 0
        return self.group(self._run_value(entry.get("block_runs", []), line))

    def has_block(self, path: str, line: int) -> bool:
        entry = self.entry(path)
        if not entry:
            return False
        return self._run_value(entry.get("block_runs", []), line) != EMPTY

    def _field(self, path: str, field: str) -> int:
        entry = self.entry(path)
        if not entry:
            return 0
        return self.group(entry.get(field, EMPTY))

    def tests_in_module(self, path: str) -> int:
        """Tests that ran any line of this file."""
        return self._field(path, "module")

    def tests_opening(self, path: str) -> int:
        """Tests that opened this file as data."""
        return self._field(path, "openers")

    def tests_importing(self, path: str) -> int:
        """Tests that imported the module this file provides."""
        return self._field(path, "importers")

    def tests_defined_in(self, path: str) -> int:
        """Tests that live in this file, whether or not they ran its lines."""
        return self._field(path, "own_tests")

    def ast_fingerprint(self, path: str) -> str | None:
        entry = self.entry(path)
        return entry.get("ast") if entry else None

    def tests_in_package(self, package: str) -> int:
        return self.group(self.meta["packages"].get(package, EMPTY))

    def knows(self, path: str) -> bool:
        return ("f/" + path + ".json") in self._names

    def all_tests(self) -> int:
        return (1 << self.test_count) - 1


def bits_to_numbers(bits: int) -> list[int]:
    """The test numbers set in a bitmask, ascending."""
    numbers = []
    while bits:
        low = bits & -bits
        numbers.append(low.bit_length() - 1)
        bits ^= low
    return numbers
