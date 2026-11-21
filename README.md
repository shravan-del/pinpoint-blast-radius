# Pinpoint — Blast Radius

Pinpoint's entry to the RWX **Blast Radius** track, Builders Buildathon 2026.

Given a unified diff against sqlglot at commit `eb11aec4be8cb2122afe713a4816662c3f0fb2fa`, the program
prints the tests that change could break, packed across up to 8 workers.

```sh
make sample   # build the image and score it on the 30 sample commits
make check    # output format only
```

The starter kit's own README is kept at [docs/starter-kit-README.md](docs/starter-kit-README.md).
Scoring rules: [SCORING.md](SCORING.md).

## Third-party code

- **sqlglot** (MIT), cloned in the `Dockerfile` at the pinned commit above, used at *build* time only to
  build the static maps. <https://github.com/tobymao/sqlglot>
- Build-time Python packages are pinned in `requirements.lock` (supplied by the kit) — notably
  `pytest`, `pytest-xdist`, `coverage`.
- The shipped selector runs on the Python standard library only.

Licensed MIT, see [LICENSE](LICENSE).
