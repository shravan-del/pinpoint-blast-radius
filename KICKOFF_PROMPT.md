Read CLAUDE.md first; it has the rules, limits and scoring for this project. This folder holds the unpacked
Blast Radius starter kit (blast-radius-sample.tar.gz). Work through the phases below in order. Stop and report at the end of
each phase with the numbers.

## Phase 0: Recon (about 45 min)
1. `git init` if needed. Add an MIT LICENSE (Copyright 2026 Pinpoint) and a short README stub
   ("Pinpoint's entry to the RWX Blast Radius track, Builders Buildathon 2026").
   Check `gh auth status`. Then ASK me before creating the public GitHub repo `pinpoint-blast-radius`.
2. Read SCORING.md and every file in the kit. Tell me:
   - anything that differs from CLAUDE.md;
   - exactly how the scorer runs the tests (pytest command, Python version, pure-Python or compiled sqlglotc);
   - the format of tests.json and how its entries map to pytest node IDs.
3. Run `make sample` on the untouched starter (linux/amd64) and record the score.
4. Analyze the 30 sample commits and write the results to NOTES.md:
   - which files each one touches (sqlglot source, tests, tests/fixtures, other);
   - what kind of edit each is;
   - how many break nothing;
   - how many tests fail per commit, and which.
   Also profile tests.json: total seconds, the 20 longest tests, seconds per test file.
   Check whether doctests (tests/test_docs.py uses `load_tests`) and subtests show up in tests.json.

## Phase 1: Safe first submission (aim to be ready about 1.5 h from now)
Keep "run every test", but pack it well:
- assign tests longest-first to the least-loaded worker;
- try every worker count k = 1..8 and keep the cheapest (busiest worker + 2·k).

This has zero miss risk. Check it with `make sample`, commit, push, and give me the commit hash to submit.
Earlier submissions win ties, so don't polish.

## Phase 2: Coverage-based selection (the core)
a. **Build the maps offline.** Write `tools/build_maps.py`. Run it in a linux/amd64 container that matches the runtime,
   and commit the outputs in a compact form (well under 20 MB):
   - per-test **line coverage** over sqlglot/ AND tests/ (coverage.py dynamic contexts, e.g. pytest-cov `--cov-context=test`);
   - per-test **import map**: which sqlglot modules are in `sys.modules` while each test runs (small pytest plugin).
     Note that dialects load on demand through `sqlglot.dialects.__getattr__`;
   - per-test **fixture/data reads**: which files under tests/ each test opens (wrap `open`).
     For example, test_transpile and test_optimizer read `tests/fixtures/*.sql`.
   Map pytest node IDs to tests.json numbers, and report any test that has no coverage context.
b. **Code that runs at import time** (module and class bodies, dialect tables like KEYWORDS, FUNCTIONS and TRANSFORMS, decorators,
   `def` lines) has no per-test context. For changes there, select every test that imported that module.
   The import map handles dialect inheritance automatically.
c. **From patch to selected tests.** Parse the unified diff yourself, handling renames, new files, deleted files and binary.
   - `sqlglot/**/*.py`: changed or deleted old-file lines map to tests through the line map.
     For pure insertions, use the lines of the enclosing function or class in the old file.
     Import-time lines use the import-map fallback.
   - `tests/**/*.py`: select the tests whose own body changed. For a change at module or class level, or to a helper,
     select every test in that file plus every importer of the helper (e.g. tests/helpers.py).
   - `tests/fixtures/**`: select the tests that read that file.
   - New modules: remember test_docs walks every package, and a new import can break collection.
   - Anything else (docs, setup files, sqlglotc, non-Python): decide from evidence. When unsure, use the safe fallback.
   - **Return `[]` only when it's provably harmless:** the Python AST of every changed .py file is identical before
     and after (compare `ast.dump` without line attributes; this includes docstrings), and no test reads the file
     as data. Commenting out code changes the AST, so that case is caught correctly.
     Remember that docstrings are run as doctests.
   - Any exception means all tests for that commit (fail safe).
d. Pack the selection with the same longest-first plus k-sweep packer.
e. Unit tests for the diff parser, the selector and the output format.
   Check that run-time CPU is small: no sqlglot or pytest import when deciding.

## Phase 3: Local evaluation beyond the 30 samples
Write `tools/mutate.py` to generate 500–2,000 varied single-change patches against eb11aec:
- operator and constant swaps, deleted statements;
- changed or removed entries in dialect tables;
- changed default arguments, renamed keys;
- docstring edits, comment-only edits, whitespace;
- fixture edits, test edits;
- unused new helpers, import changes.

Weight the mix toward the kinds of edit seen in the samples, but include others, because the final has kinds we
haven't seen. Get the real failing tests for each patch with pytest (xdist, all cores). Score our selector with the
official formula, and report the score and the misses broken down by kind of edit.
This is for EVALUATION ONLY (see the compliance section of CLAUDE.md).

## Phase 4: Tighten (only while misses stay at 0)
In priority order, each change measured on the eval set; reject any change that adds a miss:
- **Finer maps for the dialect tables:** record which keys each test actually looks up (instrument the dict lookups on
  the pinned commit), so a one-entry change doesn't select every test that imports the dialect.
- Branch/arc coverage instead of plain line coverage.
- Cut startup CPU.

## Cadence
Every hour, if you have something at least as good locally with 0 misses: commit, push, add it to SUBMISSIONS.md,
and give me the hash. I submit through the website (1 submission per hour).

Start Phase 0 now.
