"""Recorder installed on PYTHONPATH while the maps are built.

Python imports ``sitecustomize`` during start-up, before anything else runs and
in *every* interpreter, so this file is how we see what happens inside the
processes a test spawns as well as in the test process itself.

It does two jobs:

* starts coverage in subprocesses, when ``COVERAGE_PROCESS_START`` is set;
* records, through :func:`sys.addaudithook`, every file under the sqlglot
  checkout that gets opened, every module that gets imported, and every
  directory that gets listed.

A read is split into a *data* read and an *import* read by looking up the
stack for an importlib frame.  The import machinery opens a module's source
with the same mode and flags as ``open(path)`` does, so the stack is the only
thing that separates "this test reads the file as data" (a fixture, or
tests/test_typing.py reading sqlglot/typing/*.py) from "this test merely
imports the module".  Without the split, every comment-only change to a source
file would look like a change to data that every importer reads.

Records are appended the moment a path or a module is first seen rather than
flushed at exit, because a multiprocessing worker can leave through
``os._exit`` and never run its ``atexit`` handlers.

This file is a build-time tool.  It is not part of the submitted image.
"""

from __future__ import annotations

import os
import sys

_AUDIT_DIR = os.environ.get("PINPOINT_AUDIT_DIR")
_ROOT = os.environ.get("PINPOINT_REPO_ROOT")

if os.environ.get("COVERAGE_PROCESS_START"):
    try:
        import coverage

        coverage.process_startup()
    except Exception:  # noqa: BLE001 - a subprocess without coverage is survivable
        pass

if _AUDIT_DIR and _ROOT:
    _root_prefix = _ROOT.rstrip(os.sep) + os.sep
    _record_path = os.path.join(_AUDIT_DIR, f"seen-{os.getpid()}.txt")
    _seen: set[str] = set()
    _busy = False

    def _append(line: str) -> None:
        global _busy
        _busy = True
        try:
            with open(_record_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception:  # noqa: BLE001
            pass
        finally:
            _busy = False

    def _from_importlib() -> bool:
        """True when the current call came out of the import machinery."""
        try:
            frame = sys._getframe(2)
        except ValueError:  # pragma: no cover
            return False
        depth = 0
        while frame is not None and depth < 60:
            name = frame.f_code.co_filename
            if "importlib" in name:
                return True
            frame = frame.f_back
            depth += 1
        return False

    def _audit(event: str, arguments: tuple) -> None:
        # Keep this as cheap as possible: it runs on every audited event.
        if _busy:
            return
        if event in ("os.listdir", "os.scandir"):
            where = arguments[0] if arguments else None
            if not isinstance(where, str) or not where.startswith(_root_prefix):
                return
            if _from_importlib():
                return
            key = "D" + where
            if key in _seen:
                return
            _seen.add(key)
            _append("D " + where[len(_root_prefix):])
        elif event == "open":
            path = arguments[0]
            if isinstance(path, bytes):
                try:
                    path = path.decode()
                except Exception:  # noqa: BLE001
                    return
            if not isinstance(path, str):
                return  # a file descriptor, not a name
            if not path.startswith("/"):
                path = os.path.join(os.getcwd(), path)
            if not path.startswith(_root_prefix):
                return
            kind = "M" if _from_importlib() else "O"
            key = kind + path
            if key in _seen:
                return
            _seen.add(key)
            _append(kind + " " + path[len(_root_prefix):])
        elif event == "import":
            name = arguments[0]
            if not isinstance(name, str):
                return
            key = "I" + name
            if key in _seen:
                return
            _seen.add(key)
            _append("I " + name)

    sys.addaudithook(_audit)
