# AACR context-level sample (2 PRs)

A small committed sample of the context-level dataset, so its format can be read without
building it. The full dataset is built into `data/aacr/` by `data/build_dataset.sh`, as JSON
Lines (`context_levels.jsonl`, one PR per line), and is not committed.

- **File:** `context_levels.json` (9.4 MB). It is one pretty-printed JSON document,
  `{entry id: record}`. Each record is exactly one line of the dataset file. The fields are
  documented in `src/tracer_context/dataset.py` and `src/tracer_context/README.md`.
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
- **Load it:** `load_records` reads this file as well as the dataset's JSON Lines.

  ```python
  import json
  sample = json.load(open("data/aacr_sample/context_levels.json"))   # plain JSON

  from src.tracer_context.dataset import load_records, level_rows
  records = load_records("data/aacr_sample/context_levels.json")
  rows = level_rows(records.values())
  ```

- **Outcome-table sample:** `outcomes/sample_qwen3.5-0.8b/` is the review table the notebook writes for these two PRs with the `sample-local` profile. It is a pipeline demonstration with a 0.8B reviewer and judge, not results (see `outcomes/README.md`).
- **Rebuild it** from a build of those two entries, for example after a change to the level
  configs or builder versions:

  ```python
  from src.tracer_context.dataset import load_records, write_json
  write_json("data/aacr_sample/context_levels.json",
             load_records("data/aacr/context_levels.jsonl").values())
  ```
