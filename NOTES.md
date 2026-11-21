# NOTES — Pinpoint / Blast Radius

## Phase 0 — Recon (2026-11-20)

### Baseline
`make sample` on the untouched starter kit (linux/amd64):

```
Simulated: 1210.470 s
Misses:    0
```

That is 40.349 s/commit x 30: 1620 tests greedily packed onto a fixed 3 workers
(34.349 s busiest worker + 3 x 2 s).

Reference points on the same sample set:

| Strategy | Simulated | Misses |
|---|---|---|
| Starter (all tests, fixed k=3) | 1210.470 s | 0 |
| All tests, best k (k=4: 28.817 + 8) | **1104.510 s** | 0 |
| Oracle (only the truly failing tests, best k) | **94.351 s** | 0 |

So packing alone is worth ~106 s, and selection is worth up to ~1010 s more.
Two tests dominate everything: `test_max_nodes` (28.817 s) and
`test_execute_tpcds` (21.454 s). While `test_max_nodes` is selected, no commit
can cost less than 28.817 s + 2 s, which is why dropping it matters more than
dropping the other 1619 tests put together.

### Differences from CLAUDE.md
None material. Everything in CLAUDE.md matches SCORING.md and the kit README.
Three details CLAUDE.md does not spell out, worth recording:

1. The miss test is per **commit**, not per test: one missed failing test costs
   300 s for that commit, ten missed failing tests on the same commit also cost
   300 s. Cheap insurance is therefore worth most on commits where we are least sure.
2. Empty workers are dropped *before* the 8-worker check and before costing, so
   emitting padding workers is free but pointless, and `[[1,2],[]]` is one worker.
3. `score.py` costs a test as `floor(seconds * 1000 + 0.5)` ms. All arithmetic is
   integer milliseconds, so our packer must pack on integer ms, not floats, to
   agree with the scorer exactly.

### How the scorer runs the tests

Reconstructed from `Dockerfile`, `requirements.lock`, the kit README and the
sqlglot checkout at `eb11aec`:

- **Base image:** `python:3.13-slim` pinned by digest
  `sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90`.
  So **Python 3.13**.
- **Install:** `pip install -r requirements.lock`, with the sqlglot checkout on
  `PYTHONPATH` (kit README says this explicitly, and warns that sqlglot's own
  `pip install -e .[dev]` would pick newer versions).
- **Pure Python, not compiled.** `requirements.lock` contains no `sqlglotc` and
  no `sqlglotrs`. sqlglot's own `make testc` / `install-devc` builds the mypyc
  extension, but that is not what the lock file describes, and sqlglot's
  `make test` target even *hides* any stray `.so` before running. This matters:
  **coverage.py only sees pure-Python frames**, so coverage maps are valid.
  (`tests/test_sqlglotc.py` still contributes 3 tests, but it only exercises the
  `sqlglotc/setup.py` build-ext logic with `mypyc.build` mocked out; it never
  needs a compiled sqlglot.)
- **Runner:** pytest 9.2.0 (`pytest-timeout` 2.4.0 and `pytest-xdist` 3.8.0 are
  present), run from the repo root — every id in `tests.json` is rooted at
  `tests/...`. The repo has no `conftest.py` and no `[tool.pytest.ini_options]`.
- **`SKIP_INTEGRATION=0`** is set, which is also `tests/helpers.py`'s default, so
  the `tests/test_executor.py` tests run and can fail.
- A failure is a pytest failure, error, timeout, or an un-importable test file.
  pytest 9 prints `PASSED` next to `SUBFAILED` for a test whose subtest failed;
  that test counts as failed, and subtests do not get their own numbers.

### tests.json

Shape: `{"tests": [...], "skipped": [...]}`.

- 1620 tests, `number` running 0..1619 with no gaps, in listed order.
- Entry: `{"number": 0, "id": "tests/dialects/test_athena.py::TestAthena::test_athena", "file": "tests/dialects/test_athena.py", "seconds": 0.034}`.
- `skipped` is a list of 1 id:
  `tests/dialects/test_sqlite.py::TestSQLite::test_variadic_iif_execution` (no number).
- **Every id is exactly `file::Class::method`.** No parametrised ids (no `[...]`),
  no module-level function tests. So the node-id -> number map is total and
  unambiguous, and a pytest `nodeid` from a coverage context maps by string equality.
- Total recorded time: **103.047 s**. It is extremely top-heavy:
  top 10 tests = 71.7 %, top 50 = 88.2 %, top 200 = 94.5 %.
- 58 test files. The heavy ones are `test_executor.py` (31.6 s, 60 tests),
  `test_parser.py` (29.1 s, 81), `test_optimizer.py` (19.8 s, 167),
  `test_generator.py` (6.8 s, 10).
- 20 longest tests:

  | s | # | id |
  |---|---|---|
  | 28.817 | 1490 | tests/test_parser.py::TestParser::test_max_nodes |
  | 21.454 | 1106 | tests/test_executor.py::TestExecutor::test_execute_tpcds |
  | 6.686 | 1224 | tests/test_generator.py::TestGenerator::test_generator_memory |
  | 3.863 | 1107 | tests/test_executor.py::TestExecutor::test_execute_tpch |
  | 3.537 | 1123 | tests/test_executor.py::TestExecutor::test_optimized_tpch |
  | 2.533 | 1428 | tests/test_optimizer.py::TestOptimizer::test_scope |
  | 2.232 | 1440 | tests/test_optimizer.py::TestOptimizer::test_tpcds |
  | 1.963 | 1295 | tests/test_optimizer.py::TestOptimizer::test_annotate_funcs |
  | 1.406 | 1382 | tests/test_optimizer.py::TestOptimizer::test_qualify_columns |
  | 1.364 | 1082 | tests/test_executor.py::TestExecutor::test_agg_order |
  | 1.331 | 1361 | tests/test_optimizer.py::TestOptimizer::test_optimize_in_nulls |
  | 1.298 | 1541 | tests/test_schema.py::TestSchema::test_cache_does_not_retain_queries |
  | 1.016 | 1343 | tests/test_optimizer.py::TestOptimizer::test_merge_subqueries |
  | 1.006 | 1313 | tests/test_optimizer.py::TestOptimizer::test_canonicalize |
  | 0.905 | 1415 | tests/test_optimizer.py::TestOptimizer::test_qualify_struct_star_long_cte_chain |
  | 0.886 | 1560 | tests/test_serde.py::TestSerde::test_recursion |
  | 0.686 | 1358 | tests/test_optimizer.py::TestOptimizer::test_optimize_correlated_all |
  | 0.662 | 773 | tests/dialects/test_snowflake.py::TestSnowflake::test_snowflake |
  | 0.596 | 1434 | tests/test_optimizer.py::TestOptimizer::test_simplify |
  | 0.506 | 1561 | tests/test_serde.py::TestSerde::test_serde |

### Doctests and subtests — NOT in tests.json

The checkout has 60 `tests/**/test_*.py` files; `tests.json` names 58. The two
missing ones are the two that use the `unittest` **`load_tests` protocol**:

- `tests/test_docs.py` — walks every `sqlglot.*` package and adds a
  `doctest.DocTestSuite` for each.
- `tests/test_integration_loader.py` — discovers
  `sqlglot-integration-tests/tests/sqlglot`, a git submodule that is not fetched.

**pytest does not implement `load_tests`.** It collects `unittest.TestCase`
subclasses directly, so both files collect zero tests. That is consistent with
`tests.json`: no doctest ever appears, and no id has the `[...]` of a
parametrised case.

**Confirmed empirically** in the lab image: `python -m pytest --collect-only -q tests`
collects **1621** tests, and that set is exactly the 1620 numbered ids plus the
1 `skipped` id, in the same order. `test_docs.py` and `test_integration_loader.py`
contribute zero collected tests. Python is **3.13.18** and
`glob("sqlglot/**/*.so")` is empty, so sqlglot really is pure Python there.

Consequence: a change that only touches a **docstring** cannot fail any numbered
test through doctests.
That is a meaningful relaxation of the "docstrings are run as doctests" warning
in the kickoff, but it is exactly the kind of assumption the hidden final set
could punish, so the plan is to confirm it by running pytest in the lab image
and to keep docstring-only edits behind the same AST check as everything else
(an `ast.dump` comparison *does* see docstring changes, so the conservative
behaviour is the default and this is only an optimisation we may add later).

Subtests: sqlglot uses `self.subTest(...)` heavily, but a subtest never gets its
own number; the parent test is one entry.

### The 30 sample commits

All 30 are tiny: one hunk per file, one line changed (`comment` kind inserts one
line and deletes none). 24 file-touches are under `sqlglot/`, 11 under
`tests/fixtures/optimizer/`. **No sample commit touches a `tests/**/*.py` file,
no sample commit adds, deletes, renames or binary-patches a file.** The final
set is promised to contain kinds we have not seen, so the diff parser has to
handle all of those anyway.

Kinds: 16 `code`, 8 `fixture`, 3 `comment`, 3 `multi`. 12 of 30 break nothing.

| id | kind | fails | files |
|---|---|---|---|
| 2haz | code | 0 | sqlglot/dialects/dialect.py |
| 2jrq | code | 8 | sqlglot/optimizer/eliminate_joins.py |
| 2qn5 | code | 0 | sqlglot/generators/clickhouse.py |
| 2vdj | fixture | 1 | tests/fixtures/optimizer/pushdown_predicates.sql |
| 3utz | code | 37 | sqlglot/serde.py |
| 4d63 | code | 0 | sqlglot/dialects/snowflake.py |
| 535z | code | 1 | sqlglot/dialects/tsql.py |
| 5y72 | code | 0 | sqlglot/optimizer/eliminate_subqueries.py |
| 66ks | code | 0 | sqlglot/executor/context.py |
| 6vvp | fixture | 1 | tests/fixtures/optimizer/qualify_columns__invalid.sql |
| 6ze8 | code | 1 | sqlglot/parsers/dremio.py |
| 8y7j | code | 0 | sqlglot/generators/clickhouse.py |
| 986c | multi | 1 | sqlglot/dialects/singlestore.py + sqlglot/typing/snowflake.py + tests/fixtures/optimizer/unnest_subqueries.sql |
| 9u5k | comment | 0 | sqlglot/generators/sqlite.py |
| ary6 | code | 10 | sqlglot/parsers/bigquery.py |
| bap8 | comment | 0 | sqlglot/transforms.py |
| bj7g | code | 1 | sqlglot/generators/clickhouse.py |
| bvw2 | multi | 1 | sqlglot/dialects/materialize.py + tests/fixtures/optimizer/annotate_types.sql |
| egys | fixture | 1 | tests/fixtures/optimizer/simplify.sql |
| f4bn | fixture | 1 | tests/fixtures/optimizer/pushdown_cte_alias_columns.sql |
| f4xa | comment | 0 | sqlglot/generators/drill.py |
| fryt | code | 0 | sqlglot/parsers/dremio.py |
| gbav | code | 0 | sqlglot/parsers/singlestore.py |
| gbhk | fixture | 1 | tests/fixtures/optimizer/annotate_types.sql |
| j6uz | code | 2 | sqlglot/parsers/bigquery.py |
| ktz3 | code | 0 | sqlglot/expressions/ddl.py |
| mthh | fixture | 1 | tests/fixtures/optimizer/merge_subqueries.sql |
| nwnx | fixture | 1 | tests/fixtures/optimizer/pushdown_projections.sql |
| rszg | multi | 3 | sqlglot/optimizer/scope.py + sqlglot/typing/snowflake.py + tests/fixtures/optimizer/qualify_columns_ddl.sql |
| st8f | fixture | 1 | tests/fixtures/optimizer/isolate_table_selects.sql |

Patterns worth building on:

- **Every `tests/fixtures/optimizer/<name>.sql` edit breaks exactly one test**,
  the `tests/test_optimizer.py::TestOptimizer::test_<name>`-ish test that loads
  it, and nothing else. (`simplify.sql` -> `test_simplify`,
  `merge_subqueries.sql` -> `test_merge_subqueries`, and so on.) This must come
  out of a measured "which test opened which file" map, not from the names:
  `pushdown_projections.sql` breaks `test_pushdown_projection` (singular) and
  `qualify_columns_ddl.sql` breaks three tests, so the name heuristic is wrong.
- The 3 `comment` commits and the comment hunks inside the 2 relevant `multi`
  commits are AST-identical insertions, so the `ast.dump` rule returns `[]` for
  them and takes `multi` commits down to their fixture hunk alone. Note `bap8`
  inserts the comment *between* the `def` line and the docstring, which does not
  stop the string being a docstring, so the AST really is unchanged.
- Blast radius is wildly uneven: `sqlglot/serde.py` (3utz) breaks 37 tests across
  6 files, `sqlglot/parsers/dremio.py` (6ze8) breaks 1. Both are one-line edits.
- 8 of the 16 `code` commits break nothing, usually because the edited line is a
  semantics-preserving rewrite (`is` -> `==` in 2haz) or on a path no test takes.
  We cannot prove most of those harmless, so they will keep costing us; the win
  there has to come from a small, well-covered selection, not from `[]`.

### Repo layout at eb11aec (newer than the familiar sqlglot)

`sqlglot/` is split into packages: `dialects/`, `parsers/`, `generators/`,
`typing/`, `expressions/`, `optimizer/`, `executor/`, plus top-level
`parser.py`, `generator.py`, `tokens.py`, `tokenizer_core.py`, `transforms.py`,
`serde.py`, `schema.py`, `diff.py`, `lineage.py`, `jsonpath.py`, `anonymize.py`,
`planner.py`, `helper.py`, `time.py`, `trie.py`, `errors.py`.
So one dialect is spread over `dialects/x.py`, `parsers/x.py`, `generators/x.py`
and `typing/x.py`, which is why the import map has to be per-module.

`tests/helpers.py` reads fixtures with plain `open()` inside
`load_sql_fixtures` / `load_sql_fixture_pairs`, so wrapping `open` during the
coverage run will capture fixture reads.

### Environment

- Local lab image `pinpoint-lab` (`tools/lab/Dockerfile`) reproduces the labeling
  environment: same base digest, `requirements.lock`, sqlglot at `eb11aec` on
  `PYTHONPATH`, `SKIP_INTEGRATION=0`, no sqlglotc. Built `--platform linux/amd64`.
- The sqlglot checkout lives at `vendor/sqlglot` locally and is **gitignored**
  (the repo must stay under 20 MB and the checkout alone is 26 MB).
- sqlglot's own `CLAUDE.md` / `AGENTS.md` are inside `vendor/sqlglot` and are
  instructions for sqlglot contributors. They do not apply to this project.

## Phase 1 — Pack every test (2026-11-20)

Still "run every test on every commit", so a miss remains impossible. The only
change is how the tests are spread.

```
Simulated: 1104.510 s
Misses:    0
```

105.960 s better than the starter, and exactly the predicted optimum for a
selection of all 1620 tests.

**The packer.** Longest test first onto the least loaded worker (LPT), repeated
for every worker count 1..8, keeping the cheapest by the scorer's own formula
(busiest worker + 2 s per occupied worker). On the full test set it chooses
**k=4**: 28.817 s busiest + 8 s = 36.817 s/commit.

```
k=1 105.047   k=3  40.349   k=5  38.817   k=7  42.817
k=2  55.524   k=4  36.817   k=6  40.817   k=8  44.817
```

k=4 wins because `test_max_nodes` alone is 28.817 s. No split can put the
busiest worker below one test's duration, so past k=4 every extra worker is
2 s of pure loss. That also means the k sweep is not a nicety: as the selection
shrinks in Phase 2 the best k will move, and for a one-test selection it is k=1.

Details that matter:

- Packing is done in **integer milliseconds** with `math.floor(s*1000+0.5)`, the
  same arithmetic as `score.py`, so our cost and the scorer's cost agree exactly.
  A unit test checks the two rounding functions agree on 5000 values.
- Ties break on the test number, so the output is byte-identical every run.
- Empty workers are never emitted.
- LPT plus the k sweep is not optimal in general (bin packing), but a unit test
  confirms it matches exhaustive search on small awkward sets, and on the real
  set it hits the information-theoretic floor of `max_test + 2k`.

**CPU.** Measured under the scorer's own limits (`make cpu`): 0.72 s user+sys for
3 runs, so **~0.24 s per run**, and that is under amd64 emulation on an Apple
Silicon Mac, so real hardware will be lower. Against 1104.5 s of simulated time
it is noise, but it is the budget Phase 2 has to spend its index loading from.

**Fail-safe behaviour.** `selector.py` never raises. An unreadable `tests.json`
still prints one `{"commit": ..., "workers": []}` line per commit rather than
crashing, because a crash rejects the whole submission while misses only cost
300 s each. A missing `/data` prints nothing and exits 0. Both are unit-tested.

`BLAST_RADIUS_DATA` overrides `/data` so the unit tests and `make local` can run
a batch without Docker. The scorer never sets it.

**New make targets:** `make unit` (14 tests, standard library only),
`make lint` (ruff), `make local` (score without a Docker build), `make cpu`.
