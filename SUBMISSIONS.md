# Submissions

| # | Time (PT) | Commit | Local sample (simulated s) | Local sample misses | Mutant eval score | Mutant misses | Official score |
|---|-----------|--------|---------------------------|---------------------|-------------------|---------------|----------------|
| 0 | —         | *(starter kit, not submitted)* | 1210.470 | 0 | — | — | — |
| 1 | 2026-11-20 | `PENDING` | 1104.510 | 0 | — | — | *not submitted yet* |

### 1 — Phase 1: every test, packed well
Every test on every commit, so a miss is impossible. LPT packing with a sweep
over worker counts 1..8 picks k=4 (28.817 s busiest + 8 s = 36.817 s/commit).
Measured container CPU ~0.24 s/run under emulation, so the board should read
about 1104.7 s.
