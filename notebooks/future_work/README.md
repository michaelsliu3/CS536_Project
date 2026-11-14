# Future work: the outcome table with Claude as the reviewer

`claude_outcome_table.py` builds the same (PR × level) outcome table as
[`notebooks/outcome_table.ipynb`](../outcome_table.ipynb), but the frozen reviewer is a Claude
model called through the Claude API instead of `Qwen/Qwen3.8-27B` on a cloud GPU. It needs no
GPU: a laptop with `data/aacr/` and an Anthropic API key is enough.

**Status (November 13, 2026):** the code is tested with a stand-in client and against the real
SDK with a mocked network. **No request has been sent to the API yet, so there are no results.**
The project's reviewer is still Qwen3.8-27B. The roadmap (Section 8.5.2) allows a switch to
Claude only if Qwen fails criteria fixed before a pilot, and this script does not change that
decision. It makes the Claude table cheap to produce when it is wanted.

## Why the API and not a Claude chat or Claude Code window

| | Claude chat / Claude Code window | Claude API (this script) |
|---|---|---|
| What the model sees | The window's own system prompt and tools around every request | Exactly the pinned Codex rubric and one context level, in one call |
| Settings | Model and effort are session settings, not recorded per review | `claude-opus-5-5`, effort `medium`, recorded in every review file |
| Scale | 1,569 long reviews (about 62M input tokens) would run into usage limits | Message Batches: half price, most batches finish within an hour |
| Resuming and rerunning | Manual | Each reply is saved once; re-running sends only what is missing |
| Access | Claude subscription | An API key, billed per token, separate from a Claude subscription |

## What is the same as the Qwen table, and what differs

Same, because the code is shared (`src/review_outcomes/`):

- the reviewer prompt (Codex `/review` rubric as the system prompt, the request sentence plus the level's `context.md` as the user message);
- the reply parser, AACR-Bench's file-level judge metric, and the output files;
- the 256,000-token input cap, so both tables cover the same cells (`--max-input-tokens` changes it);
- identical levels are reviewed once.

Different, because of the model:

| Topic | Qwen3.8-27B | Claude through the API |
|---|---|---|
| Decoding | Greedy | No sampling settings are accepted, so a reply is not repeatable. Each reply is saved once and reused. `--replicate 1` draws a second, independent sample into its own run folder to measure run-to-run noise |
| Thinking | Off | Always on for Claude Opus 5.5; `--effort` controls its depth. Thinking is billed as output |
| Refusals | None | A safety classifier can decline a request. The reply is saved with `stop_reason: refusal`, scored as an empty review, and counted in the step's summary |
| Fallback model | None | None, on purpose. The API can route a refused request to another model, but another model's reply must never enter this reviewer's table |
| Window | 262,144 tokens | 1M tokens |

Not used, and why:

- **Structured outputs** (schema-enforced JSON): the rubric already specifies the JSON and the parser accepts it, and leaving it off keeps the request identical to the Qwen reviewer's.
- **Prompt caching:** only the 1.7k-token rubric is shared between requests, about 4% of an average request, and cache hits inside a batch are not guaranteed.

## Cost

Measured with `plan` on the current build of `data/aacr/context_levels.jsonl`, at Claude Opus 5.5's
prices on November 13, 2026 ($4 input and $20 output per million tokens; batches cost half):

| Selection | Requests | Input tokens (dataset's count) | Input cost, batch | Input cost, direct |
|---|---|---|---|---|
| All 185 built PRs × 10 levels | 1,569 | 62.2M | about $124 | about $249 |
| Pilot, `--first 15` | 122 | 7.7M | about $15 | about $31 |

- The dataset counts tokens with Qwen's tokenizer and Claude's counts differently. `count` asks the API for the exact numbers, free of charge.
- Output is billed on top and is unknown until a pilot. For scale: 3,000 output tokens per review (thinking included) would add about $47 in batch mode for the full table.
- The judge adds one short call per finding on a file that has reference comments.
- Of the 1,850 cells, 10 are above the input cap (one PR whose diff alone is about 783k tokens) and 271 repeat another level's input.

Each paid step prints its request count and cost estimate and asks before sending. After a step
it prints the tokens actually used.

## Running it

```bash
.venv/bin/python -m pip install "anthropic>=2.8,<3"
```

```bash
export ANTHROPIC_API_KEY=...   # in your shell only; never in a file in this repository
```

From the project root, with `P=notebooks/future_work/claude_outcome_table.py`:

| Step | Command | Cost |
|---|---|---|
| 1. See what would be sent | `.venv/bin/python $P plan` | none, no network |
| 2. Exact input tokens | `.venv/bin/python $P count` | none |
| 3. Check the request with one call | `.venv/bin/python $P review --first 1 --levels L01_min --mode direct` | under a dollar |
| 4. Pilot | `.venv/bin/python $P review --first 15` | see the table above |
| 5. Judge the pilot and write its table | `.venv/bin/python $P judge --first 15 --judge claude` | small |
| 6. Full table | the same two commands without `--first` | see the table above |

- **Send one request directly first (step 3).** A batch validates its requests only when it runs, so a mistake in the request shape would surface hours later. The reply is saved and reused by the later steps.
- **Batch mode is the default.** `review` uploads the requests, waits and collects. Ctrl+C stops the waiting, not the batches; running the same command again collects them. `--no-wait` submits and exits.
- **`--mode direct`** sends the requests now at standard prices, 4 at a time. It is subject to the account's per-minute token limits, which the largest levels can exceed on a new account.
- **The pilot's replies are reused** by the full run, because they are saved by request.
- **`table`** rebuilds `table.csv` from the saved records without calling the API, and `status` shows batches and saved replies.
- **Other reviewers:** `--model claude-sonnet-5-5` or `--effort high` writes to another run folder. Never merge two run folders into one table.

### The judge

The project's judge model is still undecided (roadmap Section 8.5.3), so `judge` makes you choose:

| `--judge` | What it is |
|---|---|
| `claude` | Claude through the same API (`--judge-model`, default `claude-opus-5-5`; `--judge-effort`, default `medium`). The roadmap asks for a judge from another model family than the reviewer, or at least a reported correlation with one |
| `openai` | An OpenAI-compatible server, such as vLLM serving an open model: `--judge-base-url`, `--judge-model` |
| `keyword-test` | A stand-in for pipeline tests. Its scores are not meaningful |

Identical judge prompts are answered by one call, so the same finding at several levels gets the same verdict.

## Output

Under `data/aacr/outcomes/<model>_<effort>_codex-rubric_v1/` (ignored by git, like all of `data/aacr/`):

| Path | Content |
|---|---|
| `reviews/`, `judgments/`, `table.csv`, `table.json`, `coverage.csv`, `manifest.json` | The same files as the main pipeline (see [`src/review_outcomes/README.md`](../../src/review_outcomes/README.md)). The manifest also records the model the API reported, the dates of the first and last reply, token totals and batch IDs, because API models are retired eventually |
| `api/replies/` | One file per answered request: the text, token counts, stop reason, served model, message and batch IDs |
| `api/rejected/` | Requests the API rejected as invalid, for example an input beyond the model's window. They are not sent again unless `--retry-rejected` is passed, and their cells show `error` in the table |
| `api/batches/` | One file per submitted batch. A `submitting-*.json` file left behind means an upload's outcome is unknown: check `status --account`, then either `collect --adopt <batch id>` or delete the file |
| `api/token_counts/` | Exact input token counts from `count` |
| `claude_outcome_table.log` | Everything the steps printed |

Tests, with no API calls:

```bash
.venv/bin/python -m unittest tests.test_claude_outcome_table -v
```
