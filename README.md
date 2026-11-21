# Pinpoint — Blast Radius

Pinpoint's entry to the RWX **Blast Radius** track, Builders Buildathon 2026.

Given a unified diff against sqlglot at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`,
the program prints the tests that change could break, packed across up to 8 workers.

```sh
make sample   # build the image and score it on the 30 sample commits
make unit     # our unit tests (standard library only)
make lint     # ruff
make cpu      # the CPU the container uses, under the scorer's own limits
```

## How it works

At scoring time the program imports nothing but the Python standard library —
never sqlglot, never pytest. It answers from a 1.8 MB index built offline from
the pinned commit and committed here (`maps/maps.zip`), in about 8 ms a commit.

### The index

Built by `tools/build_maps.py` inside `tools/lab/Dockerfile`, which is the
scorer's own Python 3.13 and `requirements.lock` with sqlglot on `PYTHONPATH`.
Three passes, each running **every test alone in a fresh interpreter**:

1. **Line coverage.** One run with dynamic contexts would be 20x faster and
   wrong: `setUpClass` lines land on the first test of a class, and `lru_cache`
   and sqlglot's dialect caches hide lines from every test after the first to
   reach them. Coverage follows spawned pools and subprocesses, so
   `test_execute_tpcds` carries the 311 lines of `executor/python.py` that only
   ever run in its workers.
2. **Reads, imports and directory listings**, from `sys.addaudithook`. A read
   is an *import* read when an `importlib` frame is above it on the stack;
   without that split every source file looks like data that all its importers
   read. Directory listings are how a file that did not exist at the pinned
   commit is attributed — `tests/test_typing.py` walks `sqlglot/typing/`.
3. **Class usage.** `__init__` is wrapped on every class sqlglot defines, via a
   `sys.meta_path` hook so lazily imported dialects are covered, and the whole
   MRO of each concrete class a test builds is recorded.

### The decision

The patch is parsed in-process — several files, several hunks, added, deleted,
renamed, mode-changed and binary files, C-quoted paths. For each changed `.py`
file the new version is rebuilt from the pinned source and parsed **once**,
which answers both "does it still parse" and "does it mean anything different".

Then, in order:

- **Same meaning, nothing selected.** The fingerprint is `ast.dump` without
  positions and with docstrings stripped, so comments, blank lines, layout and
  docstrings select nothing. No numbered test reads a docstring:
  `tests/test_docs.py` uses the `load_tests` protocol, which pytest does not
  implement, so it collects nothing.
- **Added an undecorated top-level `def` with a fresh name: nothing.** It binds
  a name and does nothing else. A class is *not* inert (a metaclass or
  `__init_subclass__` can register it), nor is an import, a decorated def, or a
  name that shadows a builtin.
- **Will not parse: every importer**, plus every test defined in the file.
- **A changed line maps to every test that ran any part of the *statement* it
  belongs to** — never to the tests that ran that one line. Changing one line
  of a statement changes which of its other lines run at all: the `rszg` sample
  turns `and` into `or` on the third line of a four-line condition, and a test
  that short-circuited before that line now reaches it and fails.
- **A statement in a class body** runs at import for every importer, so line
  coverage cannot separate them. It selects instead the tests that *used* the
  class: built one with it in the MRO, ran a method defined in it, or read an
  attribute of it by name. Three guards each fall back to the statement-wide
  answer — the edit must add or remove no identifier or string, the statement
  must evaluate no call (unless the changed line is inside a `lambda`, whose
  body does not run at import), and the class's instantiation must have been
  hooked.
- **A `def` signature** selects the tests that ran the body: a default is
  evaluated once at import and only a call reaching the body can notice.
- **A data file** selects the tests that opened it.
- **conftest.py, pytest.ini, tox.ini, setup.cfg, pyproject.toml** select
  everything in scope: they decide how pytest collects and runs.

Anything unexpected — a patch that will not apply, a file we have no record
of, any exception at all — selects every test for that commit. The program
never raises and prints nothing but answer lines on stdout.

The selection is packed longest-test-first onto the least loaded worker, tried
for every worker count 1..8, keeping the cheapest by the scorer's own formula,
in integer milliseconds so our cost and the scorer's agree exactly.

No answer is precomputed from a patch or from a kind of edit. Every rule is
evidence measured on the pinned commit.

### What is deliberately not here

Running tests to prove they pass is allowed and was built, measured and
rejected: see `tools/engine/`. A commit costs the busiest worker plus 2 s a
worker, so proving a cheap test passes changes nothing while an expensive one
remains — and the expensive ones cost more to run than to select
(`test_execute_tpcds`: 83.9 s of CPU to save 23.5 s). Measured end to end over
160 labeled commits it was worth about +0.01 s a commit, against a 12 MB clone
and a network dependency at rebuild time.

## Third-party code

- **sqlglot** (MIT) — used at *build* time only, to produce the index.
  <https://github.com/tobymao/sqlglot>
- Build-time tools: `coverage`, plus the kit's pinned `requirements.lock`.
- The shipped program uses the Python standard library only.

Scoring rules: [SCORING.md](SCORING.md). The starter kit's own README is at
[docs/starter-kit-README.md](docs/starter-kit-README.md).

Licensed MIT, see [LICENSE](LICENSE).
