"""A unified-diff parser.

Only the standard library, and no `patch`/`git` binary: the scoring container
has neither a network nor anything to shell out to.  It has to cope with
whatever the hidden set contains, so it handles several files per patch,
several hunks per file, added, deleted, renamed, mode-changed and binary
files, and C-quoted paths.

Anything it cannot make sense of raises, and the caller falls back to running
every test.
"""

from __future__ import annotations

import re

HUNK_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")

MODIFIED = "modified"
ADDED = "added"
DELETED = "deleted"


class PatchError(Exception):
    """The patch is not something we can reason about."""


class Hunk:
    __slots__ = ("old_start", "old_count", "new_start", "new_count", "body")

    def __init__(self, old_start: int, old_count: int, new_start: int,
                 new_count: int, body: list[str]) -> None:
        self.old_start = old_start
        self.old_count = old_count
        self.new_start = new_start
        self.new_count = new_count
        self.body = body


class FileChange:
    __slots__ = ("old_path", "new_path", "status", "binary", "hunks")

    def __init__(self, old_path: str | None, new_path: str | None, status: str,
                 binary: bool, hunks: list[Hunk]) -> None:
        self.old_path = old_path
        self.new_path = new_path
        self.status = status
        self.binary = binary
        self.hunks = hunks

    @property
    def path(self) -> str:
        """The path to reason about: where the file ended up, or where it was."""
        return self.new_path or self.old_path or ""

    @property
    def renamed(self) -> bool:
        return (
            self.status == MODIFIED
            and self.old_path is not None
            and self.new_path is not None
            and self.old_path != self.new_path
        )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (f"FileChange({self.old_path!r} -> {self.new_path!r}, {self.status}, "
                f"binary={self.binary}, hunks={len(self.hunks)})")


_C_ESCAPES = {
    "a": "\a", "b": "\b", "f": "\f", "n": "\n", "r": "\r",
    "t": "\t", "v": "\v", "\\": "\\", '"': '"',
}


def unquote_path(raw: str) -> str:
    """Undo git's C-style quoting of a path with unusual characters."""
    if not (raw.startswith('"') and raw.endswith('"') and len(raw) >= 2):
        return raw
    body = raw[1:-1]
    out = bytearray()
    index = 0
    while index < len(body):
        character = body[index]
        if character != "\\":
            out.extend(character.encode("utf-8"))
            index += 1
            continue
        index += 1
        if index >= len(body):
            raise PatchError("truncated escape in a quoted path")
        escape = body[index]
        if escape in _C_ESCAPES:
            out.extend(_C_ESCAPES[escape].encode("utf-8"))
            index += 1
        elif escape.isdigit():
            out.append(int(body[index:index + 3], 8))
            index += 3
        else:
            raise PatchError(f"unknown escape {escape!r} in a quoted path")
    return out.decode("utf-8", "replace")


def _strip_prefix(path: str) -> str | None:
    """Drop the a/ or b/ a diff puts in front of a path.  /dev/null is None."""
    path = unquote_path(path.strip())
    if path == "/dev/null":
        return None
    if path.startswith(("a/", "b/")):
        return path[2:]
    return path


def _split_git_header(line: str) -> tuple[str | None, str | None]:
    """The two paths on a `diff --git a/x b/x` line.

    A binary patch and a mode-only change carry no ---/+++ pair, so this line
    is the only place their paths appear.  Unquoted names may contain spaces,
    which makes the split ambiguous; git writes the same name twice in the
    overwhelming majority of cases, so prefer a split that agrees.
    """
    rest = line[len("diff --git "):].strip()
    if rest.startswith('"'):
        closing = 1
        while closing < len(rest):
            if rest[closing] == '"' and rest[closing - 1] != "\\":
                break
            closing += 1
        left = rest[: closing + 1]
        right = rest[closing + 1:].strip()
        return _strip_prefix(left), _strip_prefix(right)

    best: tuple[str | None, str | None] | None = None
    for position in range(len(rest)):
        if not rest.startswith(" b/", position):
            continue
        left = rest[:position]
        right = rest[position + 1:]
        pair = (_strip_prefix(left), _strip_prefix(right))
        if pair[0] == pair[1]:
            return pair
        if best is None:
            best = pair
    return best if best is not None else (None, None)


def _parse_hunks(lines: list[str], start: int, path_for_errors: str) -> tuple[list[Hunk], int]:
    hunks: list[Hunk] = []
    index = start
    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            break
        match = HUNK_HEADER.match(line)
        if not match:
            index += 1
            continue
        old_start = int(match.group(1))
        old_count = int(match.group(2)) if match.group(2) is not None else 1
        new_start = int(match.group(3))
        new_count = int(match.group(4)) if match.group(4) is not None else 1
        index += 1

        body: list[str] = []
        seen_old = 0
        seen_new = 0
        while index < len(lines):
            body_line = lines[index]
            if body_line.startswith("diff --git ") or HUNK_HEADER.match(body_line):
                break
            if seen_old >= old_count and seen_new >= new_count:
                break
            if body_line.startswith("\\"):
                # "\ No newline at end of file" belongs to the line before it.
                body.append(body_line)
                index += 1
                continue
            if body_line == "":
                # Some tools strip the single space from an empty context line.
                body.append(" ")
                seen_old += 1
                seen_new += 1
                index += 1
                continue
            marker = body_line[0]
            if marker == " ":
                seen_old += 1
                seen_new += 1
            elif marker == "-":
                seen_old += 1
            elif marker == "+":
                seen_new += 1
            else:
                raise PatchError(
                    f"{path_for_errors}: unexpected line in a hunk: {body_line[:40]!r}"
                )
            body.append(body_line)
            index += 1

        if seen_old != old_count or seen_new != new_count:
            raise PatchError(
                f"{path_for_errors}: hunk claims -{old_count} +{new_count} "
                f"but holds -{seen_old} +{seen_new}"
            )
        hunks.append(Hunk(old_start, old_count, new_start, new_count, body))
    return hunks, index


def parse_patch(text: str) -> list[FileChange]:
    """Every file a unified diff touches."""
    lines = text.split("\n")
    changes: list[FileChange] = []

    index = 0
    # Skip any commit-message preamble before the first "diff --git".
    while index < len(lines) and not lines[index].startswith("diff --git "):
        index += 1
    if index == len(lines):
        # No "diff --git" at all: maybe a plain unified diff with just ---/+++.
        if any(line.startswith("--- ") for line in lines):
            index = 0
        else:
            raise PatchError("no file sections found in the patch")

    while index < len(lines):
        if not lines[index].startswith("diff --git ") and not lines[index].startswith("--- "):
            index += 1
            continue

        header_old: str | None = None
        header_new: str | None = None
        status = MODIFIED
        binary = False
        rename_from: str | None = None
        rename_to: str | None = None
        git_old: str | None = None
        git_new: str | None = None

        if lines[index].startswith("diff --git "):
            git_old, git_new = _split_git_header(lines[index])
            index += 1

        # Extended headers, then the ---/+++ pair, then the hunks.
        while index < len(lines):
            line = lines[index]
            if line.startswith("diff --git "):
                break
            if HUNK_HEADER.match(line):
                break
            if line.startswith("new file mode"):
                status = ADDED
            elif line.startswith("deleted file mode"):
                status = DELETED
            elif line.startswith("rename from "):
                rename_from = _strip_prefix(line[len("rename from "):])
            elif line.startswith("rename to "):
                rename_to = _strip_prefix(line[len("rename to "):])
            elif line.startswith("copy to "):
                rename_to = _strip_prefix(line[len("copy to "):])
            elif line.startswith("Binary files ") or line.startswith("GIT binary patch"):
                binary = True
            elif line.startswith("--- "):
                header_old = _strip_prefix(line[4:])
            elif line.startswith("+++ "):
                header_new = _strip_prefix(line[4:])
                index += 1
                break
            index += 1

        hunks: list[Hunk] = []
        if not binary:
            hunks, index = _parse_hunks(lines, index, header_new or header_old or "?")
        else:
            while index < len(lines) and not lines[index].startswith("diff --git "):
                index += 1

        old_path = header_old
        if old_path is None:
            old_path = rename_from if rename_from is not None else git_old
        new_path = header_new
        if new_path is None:
            new_path = rename_to if rename_to is not None else git_new
        if status == ADDED:
            old_path = None
        if status == DELETED:
            new_path = None
        if old_path is None and new_path is None:
            raise PatchError("a file section names no path")

        changes.append(FileChange(old_path, new_path, status, binary, hunks))

    if not changes:
        raise PatchError("no file sections found in the patch")
    return changes


def apply_hunks(old_lines: list[str], hunks: list[Hunk], path: str) -> list[str]:
    """Rebuild the new version of a file from the old one and its hunks.

    The context lines are checked against the old file.  A mismatch means we
    have misread either the patch or the pinned source, and raising is the
    safe answer: the caller then runs every test.
    """
    out: list[str] = []
    cursor = 0  # zero-based index into old_lines
    for hunk in sorted(hunks, key=lambda h: h.old_start):
        start = hunk.old_start - 1
        if hunk.old_count == 0:
            # An insertion-only hunk sits *after* line old_start.
            start = hunk.old_start
        if start < cursor:
            raise PatchError(f"{path}: hunks overlap or are out of order")
        if start > len(old_lines):
            raise PatchError(f"{path}: hunk starts past the end of the file")
        out.extend(old_lines[cursor:start])
        cursor = start

        for body_line in hunk.body:
            if body_line.startswith("\\"):
                continue
            marker, content = body_line[0], body_line[1:]
            if marker == " ":
                if cursor >= len(old_lines) or old_lines[cursor] != content:
                    raise PatchError(f"{path}: context does not match at old line {cursor + 1}")
                out.append(content)
                cursor += 1
            elif marker == "-":
                if cursor >= len(old_lines) or old_lines[cursor] != content:
                    raise PatchError(f"{path}: removed line does not match at old line {cursor + 1}")
                cursor += 1
            elif marker == "+":
                out.append(content)
    out.extend(old_lines[cursor:])
    return out


def touched_old_lines(hunk: Hunk) -> tuple[set[int], set[int]]:
    """Which old lines a hunk changes, and where it inserts.

    Returns (changed, insertion_anchors).  `changed` holds the old line numbers
    the hunk removes or rewrites.  `insertion_anchors` holds, for every run of
    added lines with nothing removed, the old line numbers on either side of
    the gap, which is what the caller needs to find the enclosing block.
    """
    changed: set[int] = set()
    anchors: set[int] = set()
    old_line = hunk.old_start
    previous_old = hunk.old_start - 1

    # A maximal run of - and + lines is one change block.  A block that removes
    # anything is a rewrite, and the removed old lines already say where it is.
    # Only a block that is purely additions needs an insertion point.
    block_added = False
    block_removed = False

    def close_block() -> None:
        nonlocal block_added, block_removed
        if block_added and not block_removed:
            anchors.add(previous_old)
            anchors.add(old_line)
        block_added = False
        block_removed = False

    for body_line in hunk.body:
        if body_line.startswith("\\"):
            continue
        marker = body_line[0]
        if marker == " ":
            close_block()
            previous_old = old_line
            old_line += 1
        elif marker == "-":
            changed.add(old_line)
            block_removed = True
            previous_old = old_line
            old_line += 1
        elif marker == "+":
            block_added = True
    close_block()
    anchors.discard(0)
    return changed, anchors
