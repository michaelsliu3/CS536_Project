# AACR context-level sample (2 PRs)

A small committed sample of the context-level dataset, so its format can be read without
building it. The full dataset is built into `data/aacr/` by `data/build_dataset.sh` and is
not committed.

- **File:** `context_levels.jsonl`, one JSON line per PR (9.1 MB). The fields are documented in
  `src/tracer_context/dataset.py` and `src/tracer_context/README.md`.
- **PRs:** the first two AACR-Bench entries, FreeCAD/FreeCAD#19411 and #21257 (C++), with
  all ten levels each. Their input tokens run `L01` → `L10`: 27,365 → 256,337 and 13,071 →
  256,337.
- **Built:** November 8, 2026, with the content-defined levels (no per-level budgets, one
  256,339-token input cap; roadmap Section 5.5.7). That means render version 3, fetch
  version 2, codebase-memory-mcp 0.12.1 in `full` index mode, and the `Qwen/Qwen3.8-27B`
  tokenizer. Each record's `build` field has the exact versions and config hashes.
- **Contents:**
  - PR titles and descriptions from GitHub;
  - excerpts of FreeCAD's source code (FreeCAD is licensed under LGPL-2.1);
  - counts derived from AACR-Bench's reference comments, but no comment text.
- **Load it:**

  ```python
  from src.tracer_context.dataset import load_records, level_rows
  records = load_records("data/aacr_sample/context_levels.jsonl")
  rows = level_rows(records.values())
  ```

- **Staleness:** a build with changed level configs or builder versions produces different
  records. Rebuild the sample from those two entries when that happens.
