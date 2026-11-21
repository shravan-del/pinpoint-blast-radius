"""Recorder installed on PYTHONPATH while the maps are built.

Python imports ``sitecustomize`` during start-up, before anything else runs and
in *every* interpreter, so this file is how we see what happens inside the
processes a test spawns as well as in the test process itself.

It does three jobs:

* starts coverage in subprocesses, when ``COVERAGE_PROCESS_START`` is set;
* records, through :func:`sys.addaudithook`, every file under the sqlglot
  checkout that gets opened, every module that gets imported, and every
  directory that gets listed;
* records which sqlglot **classes** the test actually used, by wrapping
  ``__init__`` on every class sqlglot defines and noting ``type(self)`` the
  first time each concrete class is built.  That is what lets a change to a
  statement in a class body select the tests that use that class instead of
  every test that imports the module.

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


# ---------------------------------------------------------------------------
# Which classes did this test actually use?
# ---------------------------------------------------------------------------
#
# Line coverage cannot answer this.  A class body runs once, at import, for
# every test that imports the module, so changing one entry of a dialect table
# or of an Expression's arg_types looks identical to every importer.  What
# separates them is whether the test ever built one.
#
# Every class sqlglot defines gets its __init__ wrapped.  A class that defines
# its own __init__ is wrapped directly; one that does not inherits a wrapped
# one from an ancestor.  Either way exactly one wrapper runs per instantiation,
# and it records the concrete class's whole MRO, so asking "which tests used a
# class with C in its MRO" is a lookup.
#
# A class whose MRO holds no wrapped __init__ at all - instances built through
# __new__ only, Enum members - is reported as untrackable, and the selector
# falls back to every importer for changes in its body rather than trusting an
# empty answer.

if _AUDIT_DIR and _ROOT:
    import functools

    _CLASSES_SEEN: set = set()

    def _label(cls: type) -> str:
        return f"{getattr(cls, '__module__', '?')}:{getattr(cls, '__qualname__', '?')}"

    def _note_instance(cls: type) -> None:
        if cls in _CLASSES_SEEN:
            return
        _CLASSES_SEEN.add(cls)
        mro = [
            _label(base)
            for base in getattr(cls, "__mro__", (cls,))
            if getattr(base, "__module__", "").startswith("sqlglot")
        ]
        if mro:
            _append("C " + "|".join(mro))

    def _wrap_init(cls: type) -> bool:
        """Wrap this class's own __init__.  True when one is now in place."""
        own = cls.__dict__.get("__init__")
        if own is None:
            return False
        if getattr(own, "_pinpoint_wrapped", False):
            return True
        if not callable(own):
            return False

        @functools.wraps(own)
        def recorder(self, *arguments, **keywords):
            _note_instance(type(self))
            return own(self, *arguments, **keywords)

        recorder._pinpoint_wrapped = True
        try:
            cls.__init__ = recorder
        except Exception:  # noqa: BLE001 - a class we cannot touch is untrackable
            return False
        return True

    def _instrument(module) -> None:
        name = getattr(module, "__name__", "")
        if not name.startswith("sqlglot"):
            return
        for attribute in list(vars(module).values()):
            if not isinstance(attribute, type):
                continue
            # Only classes this module defines, so each is handled once.
            if getattr(attribute, "__module__", None) != name:
                continue
            for cls in [attribute, *_nested_classes(attribute)]:
                _wrap_init(cls)
                trackable = any(
                    getattr(base.__dict__.get("__init__"), "_pinpoint_wrapped", False)
                    for base in getattr(cls, "__mro__", (cls,))
                )
                _append(("T " if trackable else "U ") + _label(cls))

    def _nested_classes(outer: type) -> list:
        found = []
        for value in list(vars(outer).values()):
            if isinstance(value, type) and value.__qualname__.startswith(
                outer.__qualname__ + "."
            ):
                found.append(value)
                found.extend(_nested_classes(value))
        return found

    class _Finder:
        """Wraps every sqlglot module's loader so we can instrument it."""

        def find_module(self, *arguments):  # pragma: no cover - legacy API
            return None

        def find_spec(self, name, path=None, target=None):
            if not name.startswith("sqlglot"):
                return None
            import importlib.machinery
            import importlib.util

            for finder in sys.meta_path:
                if finder is self:
                    continue
                find = getattr(finder, "find_spec", None)
                if find is None:
                    continue
                spec = find(name, path, target)
                if spec is None or spec.loader is None:
                    continue
                loader = spec.loader
                if isinstance(loader, importlib.machinery.ExtensionFileLoader):
                    return None
                original_exec = loader.exec_module

                def exec_module(module, _original=original_exec):
                    if _tabletrack is not None:
                        _tabletrack.IMPORTING += 1
                    try:
                        _original(module)
                    finally:
                        if _tabletrack is not None:
                            _tabletrack.IMPORTING -= 1
                    try:
                        _instrument(module)
                    except Exception:  # noqa: BLE001
                        pass
                    if _tabletrack is not None:
                        try:
                            _tabletrack.install(module)
                        except Exception:  # noqa: BLE001
                            pass

                loader.exec_module = exec_module
                return spec
            return None

    _tabletrack = None
    if os.environ.get("PINPOINT_TABLES"):
        try:
            import tabletrack as _tabletrack

            _tabletrack.arm(os.path.join(_AUDIT_DIR, f"tables-{os.getpid()}.json.gz"))
        except Exception:  # noqa: BLE001
            _tabletrack = None

    if os.environ.get("PINPOINT_CLASSES") or os.environ.get("PINPOINT_TABLES"):
        sys.meta_path.insert(0, _Finder())
