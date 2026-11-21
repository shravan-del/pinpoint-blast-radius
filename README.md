# Pinpoint — Blast Radius

Pinpoint's entry to the RWX **Blast Radius** track, Builders Buildathon 2026.

Given a unified diff against sqlglot at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`,
the program prints the tests that change could break, packed across up to 8 workers.

```sh
make sample   # build the image and score it on the 30 sample commits
make unit     # our unit tests (standard library only)
make lint     # ruff
```

## How it decides

At scoring time the program imports nothing but the Python standard library —
never sqlglot, never pytest. It answers from a 1.7 MB index built offline from
the pinned commit and committed to this repository (`maps/maps.zip`).

**The index** (`tools/build_maps.py`, run in `tools/lab/Dockerfile`, which is the
scorer's own Python 3.13 and `requirements.lock` with sqlglot on `PYTHONPATH`):

- **Line coverage, one fresh process per test.** All 1620 tests are run
  individually under coverage.py. A single run with dynamic contexts is far
  faster and gets the answer wrong — `setUpClass` lines land on the first test
  of a class, and `lru_cache` and sqlglot's dialect caches hide lines from
  every test after the first to reach them.
- **Subprocesses included.** `test_executor.py` and `test_optimizer.py` fan out
  to spawned process pools and `test_dialect.py` shells out to `python -c`;
  coverage follows all of them, so `test_execute_tpcds`'s map holds the 311
  lines of `sqlglot/executor/python.py` that only ever run in its workers.
- **Which modules each test imported**, from the `import` audit event.
- **Which files each test read as data**, from the `open` audit event, with
  import reads separated from data reads by looking for an `importlib` frame on
  the stack. Both raise the same event with the same mode, and without the
  split every source file would look like data every importer reads.
- **Which directories each test listed**, so a file that did not exist at the
  pinned commit can still be attributed: `tests/test_typing.py` walks
  `sqlglot/typing/` with `pkgutil.iter_modules`.

**The decision** (`pinpoint/`): the patch is parsed in-process (several files,
several hunks, added, deleted, renamed, mode-changed and binary files, C-quoted
paths). For each changed `.py` file the new version is rebuilt from the pinned
source and parsed once, which answers two questions at the same time: does it
still parse, and does it mean anything different. "Means anything different" is
a hash of `ast.dump` without positions and with docstrings stripped, so
comments, blank lines, layout and docstrings select nothing. Otherwise each
changed line maps to every test that ran any part of the **statement** it
belongs to — not to the tests that ran that one line, because changing one line
of a statement changes which of its other lines run at all.

Anything unexpected — a patch that will not parse, a file we have no record of,
any exception at all — selects every test for that commit. The program never
raises and prints nothing but answer lines on stdout.

No answer is precomputed from a patch or from a kind of edit. Every rule is
evidence measured on the pinned commit.

## Third-party code

- **sqlglot** (MIT) — used at *build* time only, to produce the index.
  <https://github.com/tobymao/sqlglot>
- Build-time tools: `coverage`, plus the kit's pinned `requirements.lock`
  (`pytest`, `pytest-xdist`, `pytest-timeout`, `duckdb`, …).
- The shipped program uses the Python standard library only.

Scoring rules: [SCORING.md](SCORING.md). The starter kit's own README is at
[docs/starter-kit-README.md](docs/starter-kit-README.md).

Licensed MIT, see [LICENSE](LICENSE).
