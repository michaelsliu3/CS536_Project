# Offline outcome table

Reviews every (PR, context level) once with a frozen reviewer and scores each review with
AACR-Bench's file-level semantic metric. This is the outcome table of roadmap Section 8.5.6.
Selector training and evaluation then only look up rows. Driver:
[`notebooks/outcome_table.ipynb`](../../notebooks/outcome_table.ipynb).

| Stage | Module | Output under `data/aacr/outcomes/<run>/` |
|---|---|---|
| Find cells | `table.discover_cells` | one per (PR, level) in `data/aacr/context_levels.jsonl.gz`, from `scripts/dataset/build_dataset.py data/aacr` |
| Review | `prompts`, `backends`, `parse`, `table.run_reviews` | `reviews/<entry>/<level>.json`: raw reply, parsed findings, tokens, prompt hash, backend settings |
| Judge | `judge`, `table.run_judgments` | `judgments/<entry>/<level>.json`: per generated comment, matched references and whether new; per reference, covered |
| Table | `table.build_table`, `write_outputs` | `table.csv`/`table.jsonl` (per entry and level), `coverage.csv` (per reference and level), `manifest.json` |

- **Reviewer prompt:** OpenAI Codex's `/review` rubric, verbatim, as the system message. The user message is a one-sentence request plus the level's `context.md` (see [`configs/review_prompts/`](../../configs/review_prompts/README.md)). The rubric asks for JSON findings with a title, body, priority, confidence, and path plus line range. Each finding becomes one generated comment (`note` = title + body).
- **Parsing:** bare, fenced, or prose-wrapped JSON is accepted. Anything else is `malformed`, scored as an empty review, and reported. A reply cut off at the output limit is marked `truncated`. Paths are made repository-relative using the files named in the trace, never the reference comments.
- **Judge:** a port of AACR's `evaluation/judge.py` with the same file grouping, prompt (checked byte-identical), answer parsing, and no credit for repeats. AACR counts a failed judge call as no match; here it is recorded and retried on the next run.
- **Backends:**
  - `OpenAIChat` (any OpenAI-compatible server, such as `vllm serve`) for Qwen3.8-27B on the cloud GPU;
  - `TransformersChat` (in-process) for small-model pipeline tests;
  - `KeywordOverlapJudge`, a stand-in for pipeline tests only.

  All decode greedily.
- **Resuming:** each review and judgment carries a cache key over everything that changes its output: the input hash, prompt hash and version, model and settings, parser, references and judge. Re-running reuses matches, redoes changes, and retries errors.

**Status:**
- **Run so far:** only pipeline smoke tests with `Qwen/Qwen3.5-0.8B`, on vllm#17425 `L01`–`L02` and then on cline#5177 `L01`–`L02` read from the dataset file.
- **Open:** the judge model is undecided (roadmap Section 8.5.3).

Tests (Python only, no models):

```bash
python3 -m unittest tests.test_review_outcomes -v
```
