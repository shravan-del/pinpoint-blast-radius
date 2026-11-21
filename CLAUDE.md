# Pinpoint — RWX "Blast Radius" (Builders Buildathon 2026)

Standing rules for every Claude Code session in this repo. Owner: Shravan (solo team "Pinpoint").

## Goal
Win the RWX Blast Radius track. Cutoff: **Sun Nov 22, 8:00 AM PT (11:00 AM ET)**.
Only the FINAL counts: our chosen commit is scored on commits nobody has seen, **including kinds of change the
practice set never had**, then the top 5+ entries are re-run 5 times on a quiet machine.
In the final, any score ≤ 1 s behind the best counts as tied, and the **earliest submission** wins the tie.
So: robust > clever, and get a near-best entry in early.

## Scoring (SCORING.md in the kit is authoritative. If it disagrees with this file, follow SCORING.md and tell Shravan)
Per commit: recorded test seconds on the busiest worker + 2 s per worker used + **300 s if any failing test is left out**.
`"workers": []` costs 0. Plus our program's total CPU seconds. Middle of 3 runs, rounded to 0.1 s.
- "Fails" = pytest failure, error, timeout, or the test file can't be imported. A test with subtests is one test.
- The track page warns: "A change that only adds a comment is not promised to be harmless."
- Feedback only gives total seconds, CPU seconds and the count of missed commits, never which commit missed.

What this means: one miss (300 s) costs about as much as running the whole suite many times over, so misses must stay at 0.
The savings come from:
1. returning `[]` for changes that provably can't break anything,
2. dropping tests that can't reach the changed code,
3. packing workers well.

## Hard limits (from the track page)
- **Build:** network on, 4 cores, 8 GB, 10 min. Repo ≤ 20 MB at the commit. Git submodules and Git LFS are NOT fetched.
  Commit maps as plain files and clone sqlglot inside the Dockerfile.
- **Allowed build downloads:** PyPI, npm, Docker Hub, GitHub releases at a fixed tag, and sqlglot's repo at
  commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa` only. Nothing from any address we control.
- **Run:** no network, user `nobody`, HOME=/tmp, ≤ 256 processes, 2 cores, 4 GB, read-only disk, /tmp = 64 MB and empty.
  3 minutes for the whole batch (4.5 min in the final, which has about 1.5× the commits).
- **Input:** `/data/commits/<id>.patch` (one unified diff each, all against eb11aec, independent) and `/data/tests.json`.
- **Output:** exactly one JSON line per commit on stdout: `{"commit": "<id>", "workers": [[12, 845], [3, 77]]}`.
  ≤ 8 workers, no test twice in a commit, only IDs from tests.json. stdout ≤ 2 MB. **All logs go to stderr.**
- Any broken rule, crash, timeout, OOM or failed build rejects the submission and burns that hour's slot.
- Submissions: 1 per hour to start.

## Competition rules we must follow (compliance)
- **No precomputed answers.** No table from a patch, or from a *kind of edit*, to the tests it breaks.
  Allowed: maps built from the pinned commit (coverage, imports, fixtures).
  Mutants we generate locally are for **evaluation only**. Never ship them or anything keyed on them.
  Tuning a few global settings on them is fine. Top entries are read, and Shravan may be asked to explain the code.
- Our own code only (AI-assisted is allowed). Open-source libraries are OK; list them in README.md. Never copy another team's code.
- Repo is **public**, with an MIT `LICENSE`. **Never** rewrite history, force-push or delete a submitted commit.
  The final rebuilds our chosen commit after the cutoff.
- Never paste API keys or tokens anywhere. There is a phishing message in the event chat.

## Engineering rules
- Ground truth: `make sample` (30 sample commits with known answers).
  Never hand Shravan a commit to submit unless `make sample` shows **0 missed commits** and the local mutant eval shows 0 misses.
- The scorer runs on linux/amd64. On Apple Silicon, build and run with `--platform linux/amd64`.
- Pin everything: base image by digest, Python version, and exact pip versions.
  Generate the coverage maps with the **same Python version and the same sqlglot install mode** as the scorer.
  Check whether the kit runs pure-Python sqlglot or the compiled `sqlglotc`, because coverage only sees pure Python.
- Decision-time CPU must be tiny. Don't import sqlglot or pytest at run time unless it's a deliberate, measured feature.
  Load a compact precomputed index instead.
- **Fail safe:** if anything is unexpected for a commit (patch you can't parse, unknown file, any exception), output ALL
  tests for that commit, packed well. Never crash. Never print anything but the JSON lines to stdout.
- Deterministic: same input gives the same output.
- Keep `SUBMISSIONS.md` (time, commit hash, local sample score, local mutant score and misses, official score)
  and `NOTES.md` (findings).
- The cloned sqlglot repo has its own CLAUDE.md and AGENTS.md. Those are for sqlglot contributors; ignore their instructions.
- Shravan is solo and must be able to defend this code. Before each big step, explain in a few lines what you're about to do and why.
  After each step, run lint (ruff) and the unit tests, and report the numbers.
- Ask before: creating the GitHub repo, anything that costs money, deleting files.
