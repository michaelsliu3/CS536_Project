# Outcome-table sample (pipeline demonstration, not results)

`sample_qwen3.5-0.8b/` is what `notebooks/outcome_table.ipynb` writes for the committed 2-PR
context sample (`../context_levels.json`). It was run with the `sample-local` profile on a
39 GB M4 Max laptop on November 8, 2026:

```bash
OUTCOME_PROFILE=sample-local jupyter nbconvert --to notebook --execute notebooks/outcome_table.ipynb --output-dir <scratch>
```

**The scores are not results.** The reviewer *and* the judge are `Qwen/Qwen3.5-0.8B`, a 0.8B
model, so the run only shows the real file layout and exercises every stage on real data. For
example, it credits a hallucinated finding with two reference comments. The real table uses
Qwen3.8-27B on the cloud GPU and a judge that is still to be chosen (roadmap Section 8.5.3).

| | Cells |
|---|---|
| Reviewed | 10 of 20 (every level up to 100,000 input tokens, the laptop's limit) |
| Parsed `ok` | 4 |
| `malformed` | 6 (the 0.8B model ran on to its 2,048-token output limit; scored as empty reviews) |
| `too_long` | 10 (over 100,000 input tokens; not sent) |
| Reused from an identical level | `L06_tests1` from `L05_callers1` on both PRs, and `L09` from `L08` on #19411 |

**Files:**

| Path | What it is |
|---|---|
| `manifest.json` | the profile, code commit, prompt and rubric hashes, model identity, and the input files' hashes |
| `reviews/<entry>/<level>.json` | the raw reply, parsed findings, tokens and seconds, or why the cell was not reviewed |
| `judgments/<entry>/<level>.json` | AACR's judge result: generated and matched comments, covered references, recall and precision |
| `table.csv`, `table.json` | one row per (entry, level), as CSV and as one JSON array: status, tokens, findings, recall, precision, balanced score, and human/AI split |
| `coverage.csv` | one row per (entry, level, reference comment): whether it was covered |

Reviews took 13–713 s per cell. The slowest is #19411's 98k-token `L03_file`, because the
model's linear-attention layers fall back to slow reference kernels on this Mac.
