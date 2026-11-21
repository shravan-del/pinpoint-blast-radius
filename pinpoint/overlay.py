"""A per-commit source tree that a *fresh interpreter* sees.

tests/test_executor.py and tests/test_optimizer.py submit work to a
ProcessPoolExecutor with the spawn start method.  Spawn starts a brand new
interpreter, so anything we arrange inside our own process - a meta_path
finder, a patched sys.modules, a monkeypatched loader - is simply not there in
the worker.  A worker would import the *pristine* sqlglot, the test would pass,
and we would drop a test that really fails.  That is a 300 s miss.

So the overlay is path-based and nothing else: a directory tree of real
directories holding symlinks to the pristine files, with the patched files
written as real files on top.  A child started with that directory as its cwd
and on PYTHONPATH sees the patched code the same way its parent does, because
it is just a directory.

Only the standard library; this runs at scoring time.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

# Fixed, because the bytecode cache under PYTHONPYCACHEPREFIX is keyed on the
# absolute source path and is prebuilt at image build time for exactly this
# one.  A per-commit directory name would miss the cache and make every child
# recompile the whole of sqlglot.
OVERLAY_ROOT = Path(os.environ.get("PINPOINT_OVERLAY", "/tmp/pinpoint-overlay"))
PRISTINE = Path(os.environ.get("PINPOINT_PRISTINE", "/app/pristine"))
PYCACHE = Path(os.environ.get("PINPOINT_PYCACHE", "/app/pycache"))

TREES = ("sqlglot", "tests")


def build_farm(root: Path = OVERLAY_ROOT, pristine: Path = PRISTINE) -> int:
    """Create the symlink farm.  Returns how many links were made."""
    if root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True)
    made = 0
    for tree in TREES:
        source = pristine / tree
        if not source.is_dir():
            continue
        for current, directories, files in os.walk(source):
            directories[:] = [d for d in directories if d != "__pycache__"]
            relative = Path(current).relative_to(pristine)
            (root / relative).mkdir(parents=True, exist_ok=True)
            for name in files:
                if name.endswith((".pyc", ".pyo")):
                    continue
                link = root / relative / name
                if not link.exists():
                    link.symlink_to(Path(current) / name)
                    made += 1
    return made


class Overlay:
    """The farm, plus whichever files this commit replaces."""

    def __init__(self, root: Path = OVERLAY_ROOT, pristine: Path = PRISTINE) -> None:
        self.root = root
        self.pristine = pristine
        self._replaced: list[str] = []
        self._built = False

    def ensure(self) -> None:
        if not self._built:
            build_farm(self.root, self.pristine)
            self._built = True

    def apply(self, files: dict[str, str | None]) -> None:
        """Put this commit's versions in place.  None means the file is gone."""
        self.ensure()
        self.reset()
        for relative, text in files.items():
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.exists():
                target.unlink()
            if text is not None:
                target.write_text(text, encoding="utf-8")
            self._replaced.append(relative)

    def reset(self) -> None:
        """Put the symlinks back, so the next commit starts from pristine."""
        for relative in self._replaced:
            target = self.root / relative
            if target.exists() or target.is_symlink():
                target.unlink()
            original = self.pristine / relative
            if original.exists():
                target.parent.mkdir(parents=True, exist_ok=True)
                target.symlink_to(original)
        self._replaced = []

    def environment(self) -> dict[str, str]:
        """What a child interpreter needs to see this tree, and only this tree."""
        environment = dict(os.environ)
        environment["PYTHONPATH"] = str(self.root)
        environment["PYTHONPYCACHEPREFIX"] = str(PYCACHE)
        environment["SKIP_INTEGRATION"] = "0"
        environment["HOME"] = "/tmp"
        # numpy, pandas and duckdb start thread pools otherwise, which makes
        # forking unsafe and costs CPU we are charged for.
        for variable in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS",
                         "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS",
                         "VECLIB_MAXIMUM_THREADS"):
            environment[variable] = "1"
        return environment


if __name__ == "__main__":  # pragma: no cover - build-time helper
    import sys

    where = Path(sys.argv[1]) if len(sys.argv) > 1 else OVERLAY_ROOT
    print(build_farm(where))
