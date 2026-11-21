# Removed per-hunk level configs, kept for tests

`H05_callers1` to `H11_pr_summary` were removed from `configs/levels_hunk/` on November 21, 2026:
only `H01_min`, `H02_function`, `H03_file` and `H04_callees1` are kept for new builds. These copies
are not used by any build. `tests/test_hunk_context.py` loads them after the four kept levels, to
keep testing builder features that only wider levels use (callers, tests, guide files, search hits,
depth 2, and the other-hunks summary of `H11_pr_summary`). They are unchanged except that
`H05_callers1` inherits the kept `H04_callees1` by its path in `configs/levels_hunk/`.
