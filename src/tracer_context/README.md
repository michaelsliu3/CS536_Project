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
   line ranges and caller links with graph queries. Names lose the tool's path-derived
   prefix, so traces compare across machines. Saved as `trace.json`.
2. **Select** (`build.py`): type items as changed / callee / caller / test / doc; drop
   module and file items and built-ins; keep only the innermost changed symbols; merge
   duplicates; apply per-kind hop and item limits.
3. **Rank**: sort by the configured keys, then path, line and name, so ties never depend
   on tool output order.
4. **Pack**: render code from the checkout with fixed ranges (decorators/annotations
   added, Markdown sections extended to the next heading). Oversize items show windows
   around changed lines or their first lines. Pass 1 fills the token budget in rank
   order within each kind's share; pass 2 gives leftover budget to the next-ranked items.
   No lines are shown twice.

Outputs in `--out`: `trace.json`, `context.md` (the context text, in rank order), and
`selection.json` (config and hash, tokenizer, tokens per kind, every included item with
rank and tokens, and every excluded item with the reason).

## Controlling the amount of context

All settings are in [`configs/tracer_context.yaml`](../../configs/tracer_context.yaml).
Unknown or missing keys are errors.

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
