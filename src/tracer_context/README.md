# Tracer context builder

Builds a review context for one PR from [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp)
(arXiv 2603.27277), with a config that controls how much context is fetched and kept.
It prepares inputs only; it does not run a reviewer model.

```bash
.venv/bin/python scripts/build_tracer_context.py \
  --repo data/codebase_memory_trial/aacr/vllm-17425 --base base \
  --out data/codebase_memory_trial/context/vllm-17425
```

The checkout must have the PR head checked out with a clean worktree. `--base` is the
PR's base branch or commit; the tool diffs from the merge base (`base...HEAD`).

## Pipeline

1. **Trace** (`cbm.py`): index the checkout into a fresh cache with a pinned worker count,
   run `detect_changes` inbound (callers, tests, docs) and outbound (callees), then fetch
   line ranges and caller links with graph queries. It also records the diff (`-U3`), the
   repository's guide files (README, CONTRIBUTING, AGENTS.md, CLAUDE.md, `.cursor/rules`,
   ...) and whole-word `git grep` hits for the changed symbols' names. Names lose the
   tool's path-derived prefix, so traces compare across machines. Saved as `trace.json`.
2. **Select** (`build.py`): type items as changed symbol / changed file / callee / caller /
   test / text-search window / guide file / doc section; drop module and file nodes and
   built-ins; keep only the innermost changed symbols; merge duplicates; apply per-kind
   hop and item limits.
3. **Rank**: sort by the configured keys, then path, line and name, so ties never depend
   on tool output order.
4. **Pack**: render code from the checkout with fixed ranges (decorators/annotations
   added, Markdown sections extended to the next heading). Oversize items show windows
   around changed lines or their first lines. Pass 1 fills the token budget in rank
   order within each kind's share; pass 2 gives leftover budget to the next-ranked items.
   No lines are shown twice.

Outputs in `--out`: `trace.json`, `context.md` and `selection.json`.

- **`context.md`:** the review input. It has up to three sections: `## Pull request` (title and description, with HTML comments removed), `## Diff`, and `## Repository context` (the packed items, in rank order). The config's `input` section switches the first two on.
- **`selection.json`:** the config and its hash, the tokenizer, and token counts for each section and each kind. It also lists every included item (with the line segments shown) and every excluded item with the reason.

The token budget covers only the repository context.

## Nested context levels

`configs/levels/L01_min.yaml` … `L10_all.yaml` define ten levels, from PR text plus diff up to everything traced to three hops. Each file inherits the previous one (`inherit:`) and names it as `level.parent`. They all build on `configs/tracer_levels_base.yaml`, so they share one `tracer` section.

When a level is packed, its parent's items go in first with exactly the lines the parent showed. A whole changed file absorbs the parts of it the parent showed. So every level shows every line of its parent, and `status.json` records a check of that per level.

Build levels with the batch runner, which packs parents first. The single-PR script refuses configs that have a parent:

```bash
.venv/bin/python scripts/run_tracer_batch.py --entries vllm-project__vllm__17425 $(for f in configs/levels/L*.yaml; do printf -- '--config %s ' "$f"; done)
```

On vllm#17425, the fixed budgets bound every level. Items left over from one level used up the next level's extra budget, so several levels lack their named kind; see the roadmap, Section 5.5.4. Treat the budgets as provisional.

## Many PRs: the AACR-Bench batch runner

`scripts/run_tracer_batch.py` runs the same pipeline over AACR-Bench entries, one PR at a
time (the tool allows one active cache at a time):

```bash
.venv/bin/python scripts/run_tracer_batch.py --list --languages Go          # plan only
.venv/bin/python scripts/run_tracer_batch.py --entries ollama__ollama__12185,cline__cline__5177
```

Select entries with `--entries` (IDs `owner__repo__number`, or 0-based positions in the
file), `--languages` or `--limit`. The whole dataset needs `--all`. For each entry it:

1. Fetches only the PR's base and reviewed commits into one clone per repository
   (`data/aacr_tracer/repos/`), deepening history until they share an ancestor. It falls
   back to GitHub's `pull/<n>/head` ref for force-pushed commits, then checks out the
   reviewed commit clean.
2. Records checks (`status.json` → `checks`):
   - diff size versus `change_line_count`;
   - whether the reference comments' lines exist in the reviewed files;
   - whether right-side comments fall inside the shown diff (changed lines ±3).

   Only counts are stored, never comment text.
3. Traces once and packs every `--config` from that trace, so all configs must share one
   `tracer` section. Vary depth per config with `selection.limits`.

Outputs go to `data/aacr_tracer/entries/<id>/` (`status.json`, `trace.json`,
`<level>/context.md`, `<level>/selection.json`, and `pr.json` when a config includes PR
text), plus `data/aacr_tracer/summary.csv`. PR titles and descriptions come from the GitHub
API through `gh`. They are the current versions; `pr.json` flags descriptions edited after
the reviewed commit.
Re-running reuses traces and packs whose inputs are unchanged, so an interrupted batch
resumes. A failed entry is recorded with its stage and error, and the batch continues
(`--stop-on-error` stops it).

`diff_lines` often differs from `change_line_count`: the record's count matches the final
PR, while `target_commit` can be an earlier version. In ollama/ollama#12185 the reviewed
commit has 197 changed lines and the final PR 64.

The folder-per-PR runner above is for inspecting individual PRs in detail (full traces and
selection reports). The dataset itself is built with the one-file builder below.

## The dataset: one file for every PR and level (main path)

`scripts/build_context_dataset.py` builds the context levels for many AACR-Bench entries into
**one file**, `data/aacr_tracer/context_levels.jsonl.gz`. It uses no LLM and runs on a laptop.

```bash
.venv/bin/python scripts/build_context_dataset.py --list --languages Go     # plan and status
.venv/bin/python scripts/build_context_dataset.py --entries cline__cline__5177,ollama__ollama__12185
.venv/bin/python scripts/build_context_dataset.py --all                      # the whole dataset
```

- **Same steps per PR:** fetch the two commits, run the checks, fetch the PR text, trace once, then pack all ten levels (`configs/levels/*.yaml` by default).
- **Clones are deleted:** entries are processed grouped by repository, and each clone is deleted after its repository's last selected entry, so a repository with several PRs is fetched once. Keep clones with `--keep-repos`.
- **Resuming:** each PR is appended as one line as soon as it is done. Re-running skips PRs already built with the same commits, tool version, trace and render versions, level configs and tokenizer, and retries failed ones (`--force` rebuilds everything). After changing builder code without bumping `RENDER_VERSION` or `TRACE_FORMAT`, rebuild with `--force`. A run cut off mid-write loses at most the PR in progress.

Loading at train time:

```python
from src.tracer_context.dataset import load_records, level_rows
records = load_records("data/aacr_tracer/context_levels.jsonl.gz")   # {entry id: record}, built PRs only
rows = level_rows(records.values())    # one dict per (entry, level): tokens, item counts, text
```

Each line (format `aacr-context-levels/v1`, documented in `dataset.py`) is one PR:

| Field | Contents |
|---|---|
| `id`, `index`, `url`, `repo`, `language`, `source_commit`, `target_commit`, `merge_base` | identity |
| `status`, `failed_stage`, `error` | `ok`, or where and why it failed |
| `levels` | per level: the review input text, input/PR/diff/context tokens, budget, item counts by kind, a compact list of included items (kind, hop, file, lines, tokens, name), and the nesting check |
| `stats` | input-side counts for selector features: diff size and hunks, changed symbols by type, callers/callees/tests per hop, guide files, text-search names and hits, graph size, PR text length |
| `pr` | the PR title and description, and whether the description was edited after the reviewed commit |
| `known_paths` | files named in the trace, used to clean the reviewer's paths |
| `build`, `seconds`, `warnings` | versions and config hashes, timings per step, tool warnings |
| `audit` | checks that use the reference comments or the final PR's size. **For auditing only, never as selector inputs** |

Reference comment text is never stored in this file; scoring reads it from the AACR file.

## Controlling the amount of context

The standalone default is [`configs/tracer_context.yaml`](../../configs/tracer_context.yaml) (repository context only, 8k tokens); the levels are described above. A config can start with `inherit: <file>` to take another config's settings and override some. Unknown or missing keys are errors.

| Section | Controls | Re-trace needed |
| --- | --- | --- |
| `tracer` | walk depth and link types the tool follows | yes |
| `selection` | dropped labels, test paths, per-kind `max_hop` / `max_items` | no |
| `ranking` | sort keys and kind/link priorities | no |
| `packing` | `token_budget`, per-kind `kind_shares`, line limits, oversize policy | no |

`--budget N` overrides the budget, and `--skip-trace` reuses `trace.json`. A budget sweep
therefore costs one trace plus a few seconds per budget. `--skip-trace` refuses if the
`tracer` settings or the checkout's HEAD changed.

Token counts use the tokenizer in `packing.tokenizer`, loaded offline only, and count the
context text (each item plus its separator), not the diff or prompt. `selection.json`
records the tokenizer's cached revision. The reviewer's tokenizer (`Qwen/Qwen3.8-27B`)
needs a one-time download of its tokenizer files (about 44 MB, no weights):

```bash
.venv/bin/hf download Qwen/Qwen3.8-27B tokenizer.json tokenizer_config.json vocab.json merges.txt chat_template.jinja config.json
```

Other tokenizers give different counts: Qwen2.5-Coder's counted about 5% fewer tokens on
vLLM code, so do not report them as reviewer tokens.

## Known limits

- The tool's outbound walk follows only `CALLS` links, and `direction: both` is broken in
  v0.12.1, so the builder runs inbound and outbound separately.
- Calls on objects the tool cannot type are missed. For example, no `logger.warning_once(...)`
  call in vLLM links to the logger implementation.
- One worker builds a slightly different graph than two or more, so `tracer.workers` is pinned.
- The defaults (budget, shares, limits, ranking) are starting points, not tuned values.
  Tune them on validation PRs only.

## Tests

```bash
python3 -m unittest tests.test_tracer_context -v
```

These need only Python: synthetic traces, a temporary repo, and a character-count
stand-in for the tokenizer. No tool, model, or network.
