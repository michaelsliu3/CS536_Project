# Outcome table, first reviewer run (November 14, 2026)

**Status: the review stage is complete for all 185 PRs and 10 context levels. The first pass left 13.7% of the cells with a reply the parser could not read; a retry rule decided, implemented and run on November 14 brought that to 1.7% (Sections 7 and 8). The judge model is chosen but the judge stage has not run, so no score and no table exists yet.**

This document records what the first full run of the frozen reviewer produced, what went wrong in it and why, and what is still open. Every number below was computed from the saved review files and the run log.

## 1. What was run

| Item | Value |
|---|---|
| Reviewer | `Qwen/Qwen3.8-27B`, revision `37561816e66b5b9153b1b64c10968c18c1e6f4a0`, served by vLLM 0.33.0 |
| Machine | one cloud NVIDIA RTX PRO 6000 (96 GB), 22 CPU cores, 110 GB RAM |
| Settings | one call per cell, temperature 0, thinking off, at most 4,096 output tokens, at most 256,000 context tokens, 8 calls in parallel |
| Prompt | `codex-rubric-4e4767f+one-call-request-v1`, parser `codex-json-v1` |
| Input | `data/aacr/context_levels.jsonl` (sha256 `b06dee5e…`), 185 PRs × 10 levels = 1,850 cells |
| Code | commit `c704809`, `scripts/outcome/run_reviews.py` with the notebook's `cloud` profile |
| Run name | `qwen3.8-27b_codex-rubric_v1` |

A level whose input is identical to another level of the same PR is reviewed once and the reply is shared, so 1,850 cells needed 1,569 model calls. A three-PR timing run made the first 25 calls (10m31s); the full run made the other 1,544.

## 2. Result in numbers

| Measure | Value |
|---|---|
| Wall time, full run | 5h48m (plus 10m31s for the timing run) |
| Model calls | 1,569, none failed |
| Context tokens sent | 59.5 million (62.2 million including the rubric and request) |
| Output tokens | 2.07 million; mean 1,322 and median 711 per call |
| Throughput | about 2,760 input tokens per second |
| Findings in usable cells | 1,340 in 1,586 cells, 0.84 per cell |

| Cell status | Cells | Share |
|---|---|---|
| Usable review (`ok`) | 1,586 | 85.7% |
| Malformed reply (`malformed`) | 254 | 13.7% |
| Too long to send (`too_long`) | 10 | 0.5% |

The ten `too_long` cells are all of `ollama__ollama__9654`, whose input exceeds 256,000 tokens at every level.

## 3. The malformed replies

A reply is malformed when it does not contain the JSON answer the prompt asks for, so no finding with a file and a line range can be read from it. It is a statement about the reply's format, not about the quality of the model's reasoning. The pipeline counts a malformed cell as a review with no findings.

### 3.1 How many, by kind

The table counts distinct replies (1,569). The cell counts differ slightly because some cells share a reply.

| Kind of reply | Replies | Cells |
|---|---|---|
| Usable, finished normally | 1,334 | 1,576 |
| Usable, but cut off at the output limit | 10 | 10 |
| **Malformed: cut off at the output limit** | **171** | **193** |
| **Malformed: finished, but without the JSON** | **54** | **61** |
| Total | 1,569 | 1,840 |

So 181 replies were cut off (11.5% of all replies), and 171 of them are malformed. 54 more ended by themselves without the JSON. Together that is the 225 malformed replies behind the 254 malformed cells.

**The 171 cut-off malformed replies:**

| What was written before the cut | Replies |
|---|---|
| Reasoning only; the JSON was never started | 152 |
| JSON started, cut inside the first finding | 8 |
| JSON with at least one complete finding, cut later | 9 |
| JSON with an empty findings list, cut in the closing explanation | 2 |

**The 54 replies that finished without the JSON** (classified by reading each one):

| What the reply does | Replies |
|---|---|
| Emits a tool call (asks to run a command) and stops | 16 |
| Concludes in prose that there is nothing to flag | 18 |
| Identifies an issue but never writes the JSON | 16 |
| Stops with no verdict | 4 |

Of the 16 that identify an issue, 5 state it in a sentence or a Markdown list, and 11 stop right after announcing the output ("Let me write the findings."). Of the 4 with no verdict, 2 consist of a single sentence announcing the review.

**What this means for scoring as the data stands:**

- **Already counted correctly (20 replies):** the 18 that conclude there is nothing to flag and the 2 with an empty findings list. "No findings" is the right reading.
- **Partly readable (9 replies):** the complete findings could be read by hand, but any later ones are lost.
- **No usable answer (196 replies):** everything else. Nothing in the saved data gives a finding with a file and a line range.

**The 10 usable replies that were cut off** contain a complete JSON object followed by more reasoning: the model wrote a draft, kept revising it, and reached the limit. The parser read the draft, so these cells hold an answer the model had not settled on.

### 3.2 Cause

**The output limit and visible reasoning cause the cut-offs.**

- **The limit is 4,096 tokens per reply.** It was sized so that the largest allowed input still fits the model's 262,144-token window: 256,000 context tokens + 1,710 for the rubric and request + 4,096 for the reply = 261,806.
- **The model reasons in its answer.** Thinking is off, so there is no separate space for reasoning. The model writes its analysis first and the JSON last, although the rubric asks for the JSON with no extra prose.
- **Long reasoning uses up the whole limit.** The median reply is 711 tokens, so most replies fit easily. When the model deliberates at length, the limit is reached before the JSON, or partway through it.
- **The limit was only needed for the largest inputs.** The median input is about 22,000 tokens, so most cells have room in the window for a much longer reply. Five of the cut-off replies have more than 240,000 input tokens and do not.

**The replies that finished without the JSON have less certain causes.**

- **Tool calls:** the rubric is OpenAI Codex's `/review` rubric, written for an agent that can explore a repository. The request says that no command can be run, and the model tried anyway. 13 of the 16 tool-call cells are at `L01_min`, and the median input of these replies is about 2,100 tokens, so the model asks for more when it is given the least.
- **Prose instead of JSON:** the model ended its turn after the reasoning. Several of these replies end on an opening code fence, where the JSON would have started. Why the model stops there has not been established.

### 3.3 Examples

- **Cut off while reasoning** (`ClickHouse__ClickHouse__73546`, `L04_callees1`): about 16,000 characters of analysis that end mid-sentence with "So the", and no JSON.
- **Tool call** (`sveltejs__svelte__16232`, `L01_min`): the whole reply announces it will look at a function and then emits a `<tool_call>` running `sed` on a source file.
- **Nothing to flag, in prose** (`ClickHouse__ClickHouse__73546`, `L01_min`): the whole reply is "The patch is correct."
- **Issue identified, JSON never written** (`microsoft__typescript-go__698`, `L01_min`): the reply describes a nil-pointer bug and ends with "Let me write up the finding."
- **Cut inside the JSON** (`n8n-io__n8n__20210`, `L09_depth2_docs`): long reasoning, then a JSON block with one complete finding, cut before the block closes.

## 4. The smallest level is affected most

| Level | Usable | Malformed | of which cut off | tool call | other, no JSON | Mean findings per usable cell |
|---|---|---|---|---|---|---|
| `L01_min` | 124 | 60 | 30 | 13 | 17 | 0.99 |
| `L02_function` | 159 | 25 | 15 | 1 | 9 | 0.74 |
| `L03_file` | 160 | 24 | 20 | 0 | 4 | 0.92 |
| `L04_callees1` | 163 | 21 | 15 | 2 | 4 | 0.85 |
| `L05_callers1` | 159 | 25 | 21 | 0 | 4 | 0.87 |
| `L06_tests1` | 162 | 22 | 19 | 0 | 3 | 0.81 |
| `L07_agent` | 164 | 20 | 18 | 0 | 2 | 0.90 |
| `L08_depth2` | 165 | 19 | 19 | 0 | 0 | 0.81 |
| `L09_depth2_docs` | 162 | 22 | 21 | 0 | 1 | 0.80 |
| `L10_all` | 168 | 16 | 15 | 0 | 1 | 0.79 |

(Counts are cells. Each level also has the one `too_long` cell.)

`L01_min` has 60 malformed cells; every other level has 16 to 25. It has more of every kind: twice as many cut-offs as most levels, and nearly all of the tool calls and of the other replies without JSON. If malformed cells stay scored as empty reviews, the minimal level will look worse than it is. That would bias the labels a selector learns from, since those labels compare levels within a PR.

Malformed cells are also concentrated by PR: 99 of the 185 PRs have at least one, and `sveltejs__svelte__15250` is malformed at all ten levels.

The mean number of findings per usable cell says nothing yet about quality. Only the judge stage can say whether a level's findings match the reference comments.

## 5. Other observations

- **Greedy decoding is not exactly repeatable on this server.** Probes before the run showed that the same request can return different text depending on whether it runs alone or beside other requests. A re-run of a cell may therefore differ from the saved reply.
- **Some replies contain more than one JSON answer.** 11 usable replies hold two or more findings objects because the model drafted and then revised. The parser reads the first one. In 8 of the 11 the first and the last differ, and in one the number of findings differs. Nothing was changed for this.
- **Duplicate findings occur.** Some reviews repeat one comment at several line ranges. They are kept as written; the judge decides what they match.
- **The run resumes safely.** Each review is written to its own file as it arrives, and a re-run sends only cells that have no saved reply.

## 6. What is not done

- **Judge stage:** not run. The judge model was decided on November 14: `Qwen/Qwen3.8-27B`, the reviewer's model on the same server, with `scripts/outcome/run_judgments.py` to run it. It has not been checked against a second judge or a human audit.
- **Table:** not built; it is assembled from the judgments.
- **Malformed cells:** repaired by the retry rule except for 31 (Section 8).

## 7. Decision: a retry rule for replies without an answer

**Decided, implemented (`src/review_outcomes/retry.py`) and run on November 14, 2026; Section 8 has the result.** The rule is part of the reviewer's definition:

1. **A reply that is cut off at the output limit is sent again with a larger limit** (16,000 tokens instead of 4,096). This applies to the 181 cut-off replies, including the 10 whose draft parsed.
2. **A reply that finished without the JSON gets one follow-up message** in the same conversation, saying that no command can be run and that the JSON is due now. This applies to the 54 replies that finished without it.
3. **A reply that still has no JSON after that counts as a review with no findings,** as now.

**How the steps combine:**

- **Each step runs at most once per cell,** the longer limit first. If the longer reply finishes without the JSON, the follow-up continues from it.
- **The last reply decides the cell.** Every reply is kept in the cell's file.
- **A step needs room in the window.** The window holds the prompt and the reply together. A step that would gain fewer than 1,024 tokens of room is skipped and recorded.

**What the rule had to do with the saved reviews** (`scripts/outcome/run_reviews.py --dry-run` before the run):

| | Replies | Input tokens |
|---|---|---|
| Sent again with a longer limit (176 with 16,000 tokens, 1 with 12,851) | 177 | 6.25 million |
| Given the follow-up message | 53 | 0.40 million |
| Skipped, no room in the window | 5 | none |
| Left alone (finished with their JSON) | 1,334 | none |

The five skipped replies are all at `L10_all`, with prompts of about 257,700 tokens: four were cut off (`FreeCAD__FreeCAD__21257`, `elastic__elasticsearch__133365`, `facebook__react__33059`, `valkey-io__valkey__1485`) and one finished without the JSON (`elastic__elasticsearch__118183`). They stay without an answer.

**Why this rule and not a new run with other settings:**

- **The usable reviews stay valid.** The rule applies to every cell, and a reply that finished with its JSON never triggers it.
- **A setting changed for the whole run discards everything.** Thinking mode, the output limit and the input cap are part of each cell's cache key, so changing one means reviewing all 1,569 inputs again.
- **Leaving the cells as they are biases the table** against `L01_min` (Section 4).

**Known limits of the rule:**

- **Inputs near the cap have no room.** These are the five skipped replies above.
- **A retried reply is a new generation.** It may reason differently from the first attempt.
- **A follow-up can act as a second review.** See Section 8.

## 8. Result of the retry run

The rule ran on the same server and model in two parts: a trial on 28 replies from 9 PRs (19m53s), then the other 202 (2h23m). No call failed. A dry run afterwards shows nothing left to retry.

| Cell status | Before | After |
|---|---|---|
| Usable review (`ok`) | 1,586 (85.7%) | 1,809 (97.8%) |
| Malformed | 254 (13.7%) | 31 (1.7%) |
| Too long to send | 10 | 10 |

**By kind of first reply** (distinct replies):

| First reply | Retried | Now with an answer | Still without |
|---|---|---|---|
| Cut off at 4,096 tokens | 181 | 164 | 17 |
| Finished without the JSON, prose | 38 | 37 | 1 |
| Finished without the JSON, tool call | 16 | 4 | 12 |

The steps taken were 177 longer-limit calls and 75 follow-ups: 53 on a first reply, and 22 on a longer reply that again finished without the JSON.

**The 30 replies still without an answer** (31 cells):

- **12 were cut off again** after using all 16,000 tokens.
- **13 answered with a tool call:** 12 that began as tool calls and repeated one after the follow-up, and 1 that began as a cut-off.
- **5 were skipped** for lack of room in the window (Section 7).

**Per level, malformed cells after the retry:**

| `L01_min` | `L02` | `L03` | `L04` | `L05` | `L06` | `L07` | `L08` | `L09` | `L10_all` |
|---|---|---|---|---|---|---|---|---|---|
| 15 | 2 | 1 | 0 | 2 | 0 | 0 | 2 | 3 | 6 |

`L01_min` went from 60 to 15, so the bias of Section 4 is much smaller but not gone. Its remaining cells are mostly the tool calls, which occur almost only at that level. The 6 at `L10_all` include the 5 skipped replies.

**Observations:**

- **The longer limit was needed.** In the trial, seven of the recovered replies were longer than the old 4,096-token limit, the longest about 11,500 tokens.
- **The follow-up does not stop tool calls.** Told that no command can be run, the model asked to read a file instead.
- **The follow-up can change a verdict.** Six first replies said only that the patch was correct. After the follow-up, three of them returned findings. For such a reply the follow-up is a second review, not a reformatting of the first.

## 9. Still open

1. **How well the judge agrees** with a second judge from another model family and with a human check.
2. **What to do with the 31 cells still without an answer.** They count as reviews with no findings, and 15 of them are at `L01_min`.
3. **Whether the parser should read the last JSON object in a reply instead of the first** (Section 5).

## 10. Where the files are

All of it is git-ignored and exists on the laptop and on the cloud server.

| Path under `data/aacr/outcomes/qwen3.8-27b_codex-rubric_v1/` | Content |
|---|---|
| `reviews/<PR>/<level>.json` | one file per cell: status, raw reply, parsed findings, token counts, timings (1,850 files) |
| `logs/reviews_20261114-181507.log` | the full run's log: one line per call, a progress line every five minutes |
| `logs/reviews_20261114-175742.log` | the three-PR timing run |
| `logs/reviews_20261115-005717.log`, `logs/reviews_20261115-011901.log` | the retry trial and the retry run |
| `manifest.json` | the settings, prompt and input hashes of the run |

The folder is 18 MB. The times in the log are the server's clock (China Standard Time).
