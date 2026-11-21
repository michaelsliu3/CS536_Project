# The project's target: a real harness's review quality at far fewer tokens (November 21, 2026)

**Status: set on November 21, 2026, for now.** The learned context selector should produce reviews of similar quality to a real coding agent's on the same hunks, while using far fewer tokens. A selector that does not improve quality is acceptable. The real agent is Codex CLI (open source) on the same model as our reviewer. How "similar" is measured is proposed below and not yet agreed in detail.

This adds to the target used so far, which compared the selector only with our own fixed context levels ([per_hunk_review_test.md](per_hunk_review_test.md), Section 20.1). That comparison stays, as a second target (Section 3 below).

## 1. Why a real harness

A coding agent gathers its own context before it reviews: it runs read-only commands in the repository, reads files and searches for definitions. That is what people would otherwise use. Matching its review quality at a small fraction of its cost is worth reporting even if the selector never beats the best fixed level.

## 2. How it is measured (proposed)

| Part | Measure |
|---|---|
| Tokens | Tokens per hunk. For the selector: the reviewer's input at the level it chose. For the agent: everything its session spent, input and output, with cached input counted separately |
| Quality | Recall of the reference comments, scored by the same judge, and the severity-weighted score ([review_score_design.md](review_score_design.md)) |
| "Similar" | Recall within one point of the agent's, checked with a paired bootstrap over hunks. To be fixed before the comparison is run |
| The agent's quality, main | The agent's own final review. It is asked to answer in our reviewer's format, so the same judge can score it. This is what a user of the agent would get |
| The agent's quality, second | Our one-call reviewer given the context the agent gathered (the level `HC1_codex`). This separates the context from the agent |

## 3. Two cautions

**The token half is met by any fixed level.** On the one hunk recorded so far (jQuery PR 3956, hunk 3):

| | Tokens |
|---|---|
| `H01_min` (no context) | 347 |
| `H03_file` and `H04_callees1` | 1,283 |
| `H10_all`, the widest level built | 5,524 |
| The context the agent gathered (`HC1_codex`) | 7,139 |
| The agent's whole session | 153,318 input (131,456 of them cached) and 16,076 output (13,569 of them thinking) |

So the claim rests on the quality half. This is one hunk; the trial below gives a real average.

**The selector still has to beat simple rules.** If giving every hunk the file also matches the agent, the target is met without the selector doing anything. So the selector is also compared with the fixed levels, with random choice and with the cheapest-first rule at equal quality, as in [per_hunk_review_test.md](per_hunk_review_test.md), Section 20. It should save tokens beyond what a simple rule saves.

## 4. The agent's level: what exists

- **Code:** `scripts/harness/record_codex.py` and `src/harness_context/codex.py`, on the branch `contextcrbench-pilot` (pull request 9). The scripts table in `scripts/README.md` describes the commands.
- **How it runs:** Codex CLI 0.171.0 runs on a checkout of the pull request at the reviewed commit, in its read-only sandbox with no network, against our vLLM server through its Responses API. Its event stream is saved, and the commands it ran with their output become the level `HC1_codex`. The level is a comparison only: a selector cannot offer it, because it exists only after a whole agent session.
- **Server setting:** vLLM must be started with tool calling on: `--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder`. With thinking off, these flags do not change our reviewer's replies.
- **Done so far:** one session, on the jQuery hunk above. It took 11 minutes and ran 9 read-only commands, all on relevant files. Its own final review came back in our format.

## 5. What it needs

- **Agent sessions on enough hunks with reference comments.** A 20-hunk trial on ContextCRBench pilot hunks is prepared. A "similar quality" claim likely needs about 100 hunks. With 6 sessions in parallel, 100 hunks take about 3 hours.
- **Judging the agent's own reviews.** They are saved in each session's folder; a step that parses and judges them is not written yet.
- **Selector results on the same hunks**, at the four kept levels (`H01_min`, `H02_function`, `H03_file`, `H04_callees1`).
