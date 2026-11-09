# Tracer context builder

Builds a review context for one PR from [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp)
(arXiv 2603.27277), with a config that controls how much context is fetched and kept.
It prepares inputs only; it does not run a reviewer model.

```bash
.venv/bin/python scripts/tracer/build_tracer_context.py \
  --repo path/to/checkout --base <base branch or commit> --out path/to/output
```

The checkout must have the PR head checked out with a clean worktree. `--base` is the
PR's base branch or commit; the tool diffs from the merge base (`base...HEAD`). For AACR
entries, the batch runner below prepares such checkouts in `data/aacr_tracer/repos/`, with
branch `aacr-base` at the merge base.

The tool binary is `data/tools/codebase-memory-mcp-0.12.1/codebase-memory-mcp` (the
macOS arm64 release build, v0.12.1, MIT), set by `tracer.binary` in the configs.
`scripts/dataset/build_dataset.py` downloads and checksum-verifies the pinned build for your platform
if it is missing. Do not run the tool's `install` command: it edits AI client configs.

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
   around changed lines or their first lines. The room is the config's `token_budget`, or
   what `max_input_tokens` (a cap on PR text + diff + context) leaves after the PR text and
   diff; with both set, the smaller one. Pass 1 fills it in rank order within each kind's
   share; pass 2 gives leftover room to the next-ranked items.
   No lines are shown twice.

Outputs in `--out`: `trace.json`, `context.md` and `selection.json`.

- **`context.md`:** the review input. It has up to three sections: `## Pull request` (title and description, with HTML comments removed), `## Diff`, and `## Repository context` (the packed items, in rank order). The config's `input` section switches the first two on.
- **`selection.json`:** the config and its hash, the tokenizer, and token counts for each section and each kind. It also lists every included item (with the line segments shown) and every excluded item with the reason.

`token_budget` counts only the repository context; `max_input_tokens` counts the whole input.

## Nested context levels

`configs/levels/L01_min.yaml` … `L10_all.yaml` define ten levels, from PR text plus diff up to everything traced to three hops. Each file inherits the previous one (`inherit:`) and names it as `level.parent`. They all build on `configs/tracer_levels_base.yaml`, so they share one `tracer` section.

When a level is packed, its parent's items go in first with exactly the lines the parent showed. A whole changed file absorbs the parts of it the parent showed. So every level shows every line of its parent, and `status.json` records a check of that per level.

Build levels with the batch runner, which packs parents first. The single-PR script refuses configs that have a parent:

```bash
.venv/bin/python scripts/tracer/run_tracer_batch.py --entries vllm-project__vllm__17425 $(for f in configs/levels/L*.yaml; do printf -- '--config %s ' "$f"; done)
```

**The levels have no per-level token budgets** (decided Nov 8; roadmap Section 5.5.7). Each level shows everything its kinds and item limits allow, up to one cap on the whole input: `max_input_tokens: 256000` in the base config. Qwen3.8-27B's 262,144-token context minus 1,710 for the prompt and 4,096 for the review is 256,338, rounded down so tokenization where the prompt and context meet cannot push a cell over the window. Item limits (`max_items`) count the level's total, its parent's items included. Each level in the dataset records `capped` (items were left out for lack of room), `fits` and `same_as_parent` (identical text; the outcome table reviews it once). Fixed budgets had made levels lack their named kind on large PRs.

## Many PRs: the AACR-Bench batch runner

`scripts/tracer/run_tracer_batch.py` runs the same pipeline over AACR-Bench entries, one PR at a
time (the tool allows one active cache at a time):

```bash
.venv/bin/python scripts/tracer/run_tracer_batch.py --list --languages Go          # plan only
.venv/bin/python scripts/tracer/run_tracer_batch.py --entries ollama__ollama__12185,cline__cline__5177
```

Select entries with `--entries` (IDs `owner__repo__number`, or 0-based positions in the
file), `--languages` or `--limit`. The whole dataset needs `--all`. For each entry it:

1. Asks GitHub's compare API for the merge base of `source...target`. In a shallow clone,
   `git merge-base` can find an older common ancestor and inflate the diff (roadmap
   Section 5.5.6). It then fetches the reviewed commit and the merge base into one clone per
   repository (`data/aacr_tracer/repos/`), deepening the reviewed commit's history until it
   reaches the merge base. It falls back to GitHub's `pull/<n>/head` ref for force-pushed
   commits, then checks out the reviewed commit clean, with `aacr-base` at the merge base.
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
selection reports). The dataset itself is built with `scripts/dataset/build_dataset.py` below.

## The dataset: one command, one file (main path)

`scripts/dataset/build_dataset.py ROOT` builds the context levels for every AACR-Bench entry into
**one file** in the dataset folder `ROOT`. It uses no LLM and runs on a laptop.

```bash
.venv/bin/python scripts/dataset/build_dataset.py data/aacr --dry-run      # setup checks and plan only
.venv/bin/python scripts/dataset/build_dataset.py data/aacr                # build (or continue) everything
.venv/bin/python scripts/dataset/clean_dataset.py data/aacr                # delete the build to start over (asks first)
```

Everything lives in `ROOT`:

| Path | What |
|---|---|
| `positive_samples.json` | AACR-Bench entries, downloaded at the pinned commit and checksum-verified if missing |
| `context_levels.jsonl` | the output: one JSON line per PR with all levels |
| `logs/build_<time>.log` | every run's log: terminal output plus debug lines and failure tracebacks |
| `.work/` | temporary clones, tool caches and (if needed) the downloaded tool |
| `.build.lock` | present while a build runs; a second build on the same folder refuses to start |

`*.log` is git-ignored repo-wide. If `ROOT` is inside this repository and not already ignored,
the script writes `ROOT/.gitignore` containing `*` (`data/` is already ignored).

- **Setup checks first,** each failing with a fix:
  - git, and `gh` logged in (it fetches the PR text);
  - Python packages;
  - the dataset file;
  - the codebase-memory-mcp tool. If it's missing, the pinned v0.12.1 build for this platform is downloaded and checksum-verified.
  - the reviewer tokenizer (tokenizer files only, downloaded if missing);
  - no other tool session running;
  - free disk space;
  - the level configs.

  `--offline` forbids every download. `--dry-run` stops after the checks and the plan.
- **Per PR, grouped by repository:** get the merge base from GitHub, fetch the reviewed commit and enough history to reach it, run the checks, fetch the PR text, trace once, pack all ten levels (`configs/levels/*.yaml` by default), and append one line. Each clone is deleted after its repository's last PR.
- **Errors don't stop the build:** a failed PR is retried (`--retries`, default 2, with a growing wait; a half-fetched clone is deleted first), then recorded with its stage and error, and the build moves on. Below `--min-free-gb` (default 10) of free disk, the build stops cleanly before the next repository.
- **Commits gone from GitHub:** 11 of the 196 reviewed commits were force-pushed away and can no longer be fetched (listed in the roadmap, Section 5.5.6). They are recorded as failed at the `merge_base` stage, without retries.
- **Progress:** each PR is logged to the terminal and the `.log` file, with counts and an estimate of the time left. A summary at the end lists the failed PRs.
- **Resuming:** re-running the same command skips PRs already built with the same commits, fetch method (`FETCH_VERSION`), tool version, trace and render versions, level configs and tokenizer, and retries failed ones (`--force` rebuilds everything). Ctrl+C, `kill -INT` or `kill -TERM` stop cleanly with exit code 130, and a crash loses at most the PR in progress. After changing builder code without bumping `FETCH_VERSION`, `RENDER_VERSION` or `TRACE_FORMAT`, rebuild with `--force`.
- **Starting over:** `scripts/dataset/clean_dataset.py ROOT` deletes what the build made: the records file, leftover rewrite files, and the clones and caches in `.work/`.
  - It also deletes the repositories the inspection runner cloned into `data/aacr_tracer/repos/` (`--runner-work` for another folder), but keeps that runner's saved results.
  - It keeps `positive_samples.json`, `logs/` and a downloaded tool, so the restart needs no downloads. `--logs` also deletes the logs.
  - `--all` deletes everything the builders wrote: all of the above in `ROOT`, and in the runner's folder its saved results (`entries/`, `summary.csv`) plus outputs older defaults left there (`context_levels.jsonl`, `outcomes/`). Other files are left alone.
  - It lists each path with its size and asks first (`--yes` skips the question, `--dry-run` only lists).
  - It refuses while a build runs on `ROOT` or the inspection runner runs, and refuses a `ROOT` with no build files unless there are runner clones to delete.
- **Exit codes:**
  - 0: all built;
  - 2: finished with failed PRs;
  - 1: setup failed or the build crashed;
  - 3: stopped for low disk;
  - 130: interrupted.
- **Subsets for testing:** `--entries`, `--languages` and `--limit`.

The file is plain JSON Lines; files from before November 8 were gzip-compressed (`.jsonl.gz`) and still load. A committed 2-PR sample is in `data/aacr_sample/context_levels.json`, one pretty-printed JSON document written by `write_json` (see its README); `load_records` reads it too.

Loading at train time:

```python
from src.tracer_context.dataset import load_records, level_rows
records = load_records("data/aacr/context_levels.jsonl")   # {entry id: record}, built PRs only
rows = level_rows(records.values())    # one dict per (entry, level): tokens, item counts, text
```

Each line (format `aacr-context-levels/v1`, documented in `dataset.py`) is one PR:

| Field | Contents |
|---|---|
| `id`, `index`, `url`, `repo`, `language`, `source_commit`, `target_commit`, `merge_base` | identity |
| `status`, `failed_stage`, `error` | `ok`, or where and why it failed |
| `levels` | per level: the review input text, input/PR/diff/context tokens, the context's room, `capped`, `fits`, `same_as_parent`, item counts by kind, a compact list of included items (kind, hop, file, lines, tokens, name), and the nesting check |
| `stats` | input-side counts for selector features: diff size and hunks, changed symbols by type, callers/callees/tests per hop, guide files, text-search names and hits, graph size, PR text length |
| `pr` | the PR title and description, and whether the description was edited after the reviewed commit |
| `known_paths` | files named in the trace, used to clean the reviewer's paths |
| `build`, `seconds`, `warnings` | versions and config hashes, timings per step, tool warnings |
| `audit` | checks that use the reference comments or the final PR's size. **For auditing only, never as selector inputs** |

Reference comment text is never stored in this file; scoring reads it from the AACR file.

**Repeatability caveat:** with `tracer.index_mode: full`, the tool's call and test links depend slightly on the folder the clone sits in. On ollama/ollama#12185 that changed the graph and the deeper levels' items (roadmap Section 5.5.6). Builds in the same folder are byte-identical, and `fast` mode was path-independent in our test. Build the whole dataset in one place, or switch to `fast`.

## Controlling the amount of context

The standalone default is [`configs/tracer_context.yaml`](../../configs/tracer_context.yaml) (repository context only, 8k tokens); the levels are described above. A config can start with `inherit: <file>` to take another config's settings and override some. Unknown or missing keys are errors.

| Section | Controls | Re-trace needed |
| --- | --- | --- |
| `tracer` | walk depth and link types the tool follows | yes |
| `selection` | dropped labels, test paths, per-kind `max_hop` / `max_items` | no |
| `ranking` | sort keys and kind/link priorities | no |
| `packing` | `token_budget` (context; null = cap only), `max_input_tokens` (whole input; null = budget only), per-kind `kind_shares`, line limits, oversize policy | no |

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

## Seeing the graph in the tool's UI

Build one PR with `--keep-index` to keep the tool's graph database in `--out/index_cache`,
then serve it:

```bash
CBM_CACHE_DIR="$PWD/path/to/output/index_cache" data/tools/codebase-memory-mcp-0.12.1/codebase-memory-mcp --ui=true
```

Open http://127.0.0.1:9749, and stop it with Ctrl+C before tracing again: the tool allows one
active cache folder at a time. Each trace indexes into an empty cache; re-indexing into an
existing cache reuses the stored graph and is not a real rebuild.

## Known limits

- The tool's outbound walk follows only `CALLS` links, and `direction: both` is broken in
  v0.12.1, so the builder runs inbound and outbound separately.
- Calls on objects the tool cannot type are missed. For example, no `logger.warning_once(...)`
  call in vLLM links to the logger implementation.
- One worker builds a slightly different graph than two or more, so `tracer.workers` is pinned.
- The defaults (cap, budgets, shares, limits, ranking) are starting points, not tuned values.
  Tune them on validation PRs only.

## Tests

```bash
python3 -m unittest tests.test_tracer_context -v
```

These need only Python: synthetic traces, a temporary repo, and a character-count
stand-in for the tokenizer. No tool, model, or network.
