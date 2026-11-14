# Outcome table, first reviewer run and first table (November 14, 2026)

**Status: the outcome table exists. Qwen3.8-27B reviewed all 185 PRs at 10 context levels, a retry rule repaired most replies that had no answer (Sections 7 and 8), 41 cells are left out (Section 9), and the same model judged every review (Section 10). The first result is that recall is about 6 to 7% at every level, so the levels barely differ in aggregate. The judge has not been checked against a second judge or a human audit, so every score here is provisional.**

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

## 6. What the first pass left undone

- **Judge stage and table:** done after this first pass; see Section 10. The judge has not been checked against a second judge or a human audit.
- **Malformed cells:** repaired by the retry rule except for 31 (Section 8), which are left out together with the 10 too-long cells (Section 9).

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

- **12 were cut off again:** 11 after using all 16,000 tokens, and 1 whose longer reply finished without the JSON and whose follow-up was then cut off.
- **13 answered with a tool call:** 12 that began as tool calls and repeated one after the follow-up, and 1 that began as a cut-off.
- **5 were skipped** for lack of room in the window (Section 7).

Section 9 goes through these one by one.

**Per level, malformed cells after the retry:**

| `L01_min` | `L02` | `L03` | `L04` | `L05` | `L06` | `L07` | `L08` | `L09` | `L10_all` |
|---|---|---|---|---|---|---|---|---|---|
| 15 | 2 | 1 | 0 | 2 | 0 | 0 | 2 | 3 | 6 |

`L01_min` went from 60 to 15, so the bias of Section 4 is much smaller but not gone. Its remaining cells are mostly the tool calls, which occur almost only at that level. The 6 at `L10_all` include the 5 skipped replies.

**Observations:**

- **The longer limit was needed.** In the trial, seven of the recovered replies were longer than the old 4,096-token limit, the longest about 11,500 tokens.
- **The follow-up does not stop tool calls.** Told that no command can be run, the model asked to read a file instead.
- **The follow-up can change a verdict.** Six first replies said only that the patch was correct. After the follow-up, three of them returned findings. For such a reply the follow-up is a second review, not a reformatting of the first.

## 9. The 41 cells that are not used

**Decided November 14, 2026: the 41 cells without a usable review are left out. They are not retried again, and they are not counted as reviews with no findings.** A missing answer is a failure of the reviewer setup, not a judgement that the code is fine, so scoring it as zero would record something the reviewer never said.

The 41 are the 31 malformed cells and the 10 too-long cells. They fall into four groups.

| Group | Cells | Can another retry help? |
|---|---|---|
| A. Input larger than the reviewer can read | 10 | No |
| B. No room left in the window for a retry | 5 | No |
| C. Cut off again after the retry | 13 | In principle, but the evidence says rarely |
| D. Answered with a tool call again | 13 | Only with a new, untested rule |

### 9.1 Group A: input larger than the reviewer can read (10 cells)

**Which:** all ten levels of `ollama__ollama__9654`.

**Why it failed:** its input is 782,761 tokens at every level, including `L01_min`, so the diff alone is that large. The model's window is 262,144 tokens and the run's input cap is 256,000. The cells were never sent, and no reply exists.

**Why a retry is not possible:** there is nothing to retry. No setting of this reviewer can read an input three times its window. Only cutting the diff would fit, and a cut diff is a different input from the one the reference comments were written for.

**Consequence:** this PR is out of the table entirely, which leaves 184 PRs.

### 9.2 Group B: no room left in the window (5 cells)

| Cell | Input tokens | First reply | Room left for a reply |
|---|---|---|---|
| `FreeCAD__FreeCAD__21257` `L10_all` | 255,989 | cut off at 4,096 | 4,445 |
| `elastic__elasticsearch__133365` `L10_all` | 255,915 | cut off at 4,096 | 4,519 |
| `facebook__react__33059` `L10_all` | 255,979 | cut off at 4,096 | 4,455 |
| `valkey-io__valkey__1485` `L10_all` | 255,971 | cut off at 4,096 | 4,463 |
| `elastic__elasticsearch__118183` `L10_all` | 255,998 | finished without the JSON (3,689 tokens) | about 560 after that reply |

**Why they failed:** these levels were trimmed to just under the 256,000-token cap when the dataset was built. With the rubric and request, the prompt is about 257,700 tokens, which leaves about 4,450 tokens of the window for the reply. The first reply used its 4,096 tokens on reasoning (or, in the last row, ended without the JSON).

**Why a retry is not possible:** the window holds the prompt and the reply together. A longer reply would have at most about 350 to 420 tokens more than the first one, and a follow-up would have to fit the first reply, the follow-up message and the answer into about 560 tokens. The rule skips a step that gains fewer than 1,024 tokens. The only way to make room is a shorter input, which would no longer be the `L10_all` level.

**Note:** the `valkey` reply was cut off while writing its JSON, a few lines before the end. It is still not used: reading it would need a hand-made exception for one cell.

### 9.3 Group C: cut off again after the retry (13 cells, 12 distinct replies)

Eleven replies used all 16,000 tokens of the longer limit without writing the JSON. One of them is shared by two levels with identical input, which makes 12 cells. A thirteenth cell took both steps and was then cut off in the follow-up.

| Cell | Input tokens | How the 16,000-token reply ends |
|---|---|---|
| `ollama__ollama__8938` `L03_file` | 12,783 | an endless run of the digit 9 |
| `sveltejs__svelte__15250` `L10_all` | 26,348 | listing "Issue 6" of many; 63% of its lines repeat earlier ones |
| `wavetermdev__waveterm__1755` `L01_min` | 18,380 | item 89 of a numbered walk through the diff; 57% repeated lines |
| `langflow-ai__langflow__6044` `L05_callers1` | 42,448 | re-deriving line numbers; 32% repeated lines |
| `langflow-ai__langflow__6044` `L08_depth2` | 72,395 | re-reading a function "one more time"; 31% repeated lines |
| `wavetermdev__waveterm__1998` `L05_callers1` | 57,831 | opening another file to look at; 28% repeated lines |
| `vllm-project__vllm__12608` `L01_min` | 3,768 | copying the diff out line by line; 25% repeated lines |
| `elastic__elasticsearch__133365` `L09_depth2_docs` | 61,128 | copying code out line by line |
| `mpv-player__mpv__16090` `L08_depth2` and `L09_depth2_docs` (same input) | 64,190 | about to state "the main bug" |
| `ollama__ollama__9746` `L09_depth2_docs` | 87,800 | debating whether its findings count as blocking |
| `wavetermdev__waveterm__1755` `L02_function` | 38,975 | tracing an authentication call |
| `immich-app__immich__16893` `L02_function` | 5,216 | see below |

**Why they failed:** the model does not converge. With thinking off it deliberates in its answer, and on these inputs the deliberation does not end: it repeats itself, copies the diff out, or keeps reopening a question it had settled. Four times the output room made no difference. Input size is not the cause, since two of these inputs are under 6,000 tokens.

**The `immich` cell:** its longer reply finished without the JSON, the follow-up asked for the JSON, and the follow-up reply reasoned again for 4,096 tokens and was cut off. So both steps ran and neither produced an answer.

**Why not retry again:**

- **More room is unlikely to help most of them.** At least seven of the eleven show the signs of a loop (heavy repetition, copying, a run of one digit). A limit of 32,000 tokens would let them loop for longer. A few might finish, but nothing in the replies says which.
- **A follow-up on a cut-off reply is a different rule.** It would put up to 16,000 tokens of unfinished reasoning back into the prompt and ask for the JSON. The one case that came close to this (`immich`) reasoned again and was cut off.
- **Each further rule moves away from the reviewer as defined.** The reviewer is one call, with two retry steps that were fixed before their results were seen. A third step chosen after seeing which cells failed would be fitted to those cells.
- **The gain is at most 12 replies** out of 1,569.

### 9.4 Group D: answered with a tool call again (13 cells)

Twelve of these began as a tool call. The thirteenth (`kestra-io__kestra__7381` `L01_min`) began as a cut-off, finished its longer reply without the JSON, and answered the follow-up with a tool call. All 13 are at `L01_min`.

| Cell | Input tokens | Tool asked for in the last reply |
|---|---|---|
| `filamentphp__filament__15267` | 356 | Read a file |
| `filamentphp__filament__15217` | 833 | Bash |
| `langflow-ai__langflow__5896` | 1,196 | Grep |
| `lvgl__lvgl__8129` | 1,645 | Bash |
| `elastic__elasticsearch__118998` | 1,872 | Grep |
| `google-gemini__gemini-cli__5793` | 1,948 | Grep |
| `immich-app__immich__17124` | 2,326 | Bash |
| `n8n-io__n8n__15057` | 2,583 | Bash (`echo "no-op"`) |
| `dotnet__aspnetcore__59322` | 2,764 | a `StructuredOutput` tool |
| `ollama__ollama__9379` | 3,070 | Bash (`cat` a file) |
| `sveltejs__svelte__16232` | 3,544 | Read a file |
| `elastic__elasticsearch__133365` | 5,356 | Read a file |
| `kestra-io__kestra__7381` | 6,319 | Grep |

**Why they failed:** these are the smallest inputs in the table, 356 to 6,319 tokens. `L01_min` gives the reviewer the diff and little else, and the rubric was written for an agent that explores a repository. With so little to go on, the model tries to fetch more. Its first reply was a tool call, the follow-up said that no command can be run, and it answered with another tool call.

**What the last replies contain:**

- **Five are only a tool call,** with no review text.
- **Eight write some analysis first** and then end in a tool call. Two of those are notable. `dotnet__aspnetcore__59322` says "I cannot run commands, so I'll reason from the diff alone" and then tries to hand in its answer through a tool named `StructuredOutput` instead of writing JSON. `n8n-io__n8n__15057` describes a defect in prose and then runs `echo "no-op"`.

**Why not retry again:**

- **The same message was already ignored.** Sending it a second time would most likely get a third tool call.
- **A stronger message is a new rule with unknown effect.** It could be written, for example to say that a tool call is not an answer, but it has not been tried.
- **The answers that exist are not in the agreed format.** A few replies contain a review in prose or inside tool-call parameters. Reading those would mean writing a second parser for 13 cells, and prose findings carry no file and line range for the judge to match on.
- **A second follow-up is a second review.** Section 8 showed the follow-up changing three of six verdicts.

### 9.5 What leaving them out means

- **How it is done.** Every row of the table carries `review_status`. A row is used only when that status is `ok`. The judge stage as written still scores a malformed reply as an empty review, so the filter has to be applied when the table is read. That filter is not written yet, because the selector code does not exist.
- **PRs affected.** 27 PRs lose at least one level. `ollama__ollama__9654` loses all ten. `elastic__elasticsearch__133365` loses three (`L01_min`, `L09_depth2_docs`, `L10_all`), and `langflow-ai__langflow__6044`, `mpv-player__mpv__16090` and `wavetermdev__waveterm__1755` lose two each. The other 22 lose one.
- **Levels affected.**

  | `L01_min` | `L02` | `L03` | `L04` | `L05` | `L06` | `L07` | `L08` | `L09` | `L10_all` |
  |---|---|---|---|---|---|---|---|---|---|
  | 16 | 3 | 2 | 1 | 3 | 1 | 1 | 3 | 4 | 7 |

  (Each count includes the one cell of `ollama__ollama__9654`.)
- **Still to settle for the selector stage.** A PR with a missing level cannot have that level chosen or compared. How labels are formed for those 26 PRs is a decision for that stage. `L01_min` is missing for 15 of them, so the comparison of the minimal level rests on 169 PRs, not 184.
- **A selection effect to report.** The missing `L01_min` cells are not random: they are the PRs where the model asked for more context. Leaving them out removes exactly the cases where the minimal level was least sufficient, which favours `L01_min` slightly. Counting them as zero would have overstated the opposite.

## 10. The judge run and the first table

### 10.1 What was run

| Item | Value |
|---|---|
| Judge | `Qwen/Qwen3.8-27B`, the reviewer's model and revision, on the same server; temperature 0, thinking off, at most 3,000 output tokens |
| Metric | AACR-Bench's file-level semantic metric: one judge call per generated comment, against the reference comments on the same file |
| Code | commit `1b1fea5`, `scripts/outcome/run_judgments.py`, 8 reviews judged at a time |
| Input | the 1,840 cells that have a review (1,569 distinct), with 1,352 distinct findings |
| Time | 15m23s, no judge errors |
| Output | `judgments/`, `table.csv` (1,850 rows), `table.json`, `coverage.csv` (14,160 rows) |

The judge was chosen on November 14. It is already loaded on the server, the stage is about 1,200 short calls, and the judge only matches each comment against a fixed list of references, so a preference for its own writing matters less than it would for a quality rating.

### 10.2 Result per level

Only the 1,809 usable cells are counted (Section 9). Recall is the share of reference comments covered; precision is the share of generated comments that match a reference. Both are pooled over PRs, as AACR does.

| Level | PRs | Mean input tokens | Findings per PR | Recall | Precision | Recall, human references only |
|---|---|---|---|---|---|---|
| `L01_min` | 169 | 6,086 | 1.04 | 7.3% | 51% | 6.7% |
| `L02_function` | 182 | 14,407 | 0.79 | 6.0% | 55% | 6.6% |
| `L03_file` | 183 | 21,925 | 0.92 | 6.0% | 48% | 5.9% |
| `L04_callees1` | 184 | 25,286 | 0.91 | 6.1% | 48% | 6.4% |
| `L05_callers1` | 182 | 29,789 | 0.87 | 6.7% | 55% | 7.3% |
| `L06_tests1` | 184 | 30,730 | 0.82 | 5.9% | 53% | 6.4% |
| `L07_agent` | 184 | 38,101 | 0.87 | 6.9% | 56% | 9.2% |
| `L08_depth2` | 182 | 49,843 | 0.81 | 6.2% | 54% | 6.4% |
| `L09_depth2_docs` | 181 | 51,118 | 0.85 | 6.6% | 54% | 6.5% |
| `L10_all` | 178 | 98,921 | 0.80 | 5.7% | 52% | 6.4% |

Over all usable cells: 1,567 generated comments, 820 of them matching a reference, and 879 of 13,883 reference slots covered (6.3%).

**What the table shows:**

- **Recall is low at every level.** The reviewer writes under one finding per PR on average, and a PR has 7.7 reference comments on average (median 5.5). It cannot cover most of them whatever context it gets.
- **More context does not raise recall in aggregate.** The range across levels is 5.7% to 7.3%, and `L10_all` has the lowest recall at 16 times the input of `L01_min`. One percentage point here is about 14 covered references, so no ordering of the levels should be read from this table.
- **About half the findings match a reference** at every level (48% to 56%).
- **More context makes the reviewer say less.** The share of reviews with no finding rises from 36% at `L01_min` to 48% at `L10_all`.

### 10.3 The same PRs at every level

The rows above cover different PRs per level. On the 158 PRs where all ten levels are usable:

| | Recall |
|---|---|
| Always `L01_min` | 7.3% |
| Always `L10_all` | 5.9% |
| Best and worst single level | 7.3% (`L01_min`) and 5.9% (`L10_all`) |
| Best level per PR, chosen with hindsight | 13.3% |

- **58 PRs have no covered reference at any level,** so no choice of level matters for them.
- **90 PRs have levels that differ** in how many references they cover. These are where a selector can gain or lose.
- **`L10_all` against `L01_min`, PR by PR:** more references covered in 21 PRs, fewer in 31, the same in 106.
- **With ties going to the lower level, `L01_min` is the best level for 101 of the 158 PRs.** Among the 90 where levels differ it is still the most frequent best level (33), followed by `L02_function` (17) and `L04_callees1` (9).

**How to read the hindsight row.** 13.3% is not what a selector can reach. It takes the best of ten reviews per PR, and a single review is noisy, so part of the gap to 7.3% is the best of ten noisy draws and not the effect of context. How much is noise is unknown until some cells are reviewed more than once.

### 10.4 What this means for the project

- **Always choosing `L01_min` is a strong baseline.** It has the highest recall at the lowest cost, so a selector has to beat it on recall, not only on tokens.
- **The signal to learn is in about 90 PRs,** which is a small set for training and cross-validation.
- **The question the table cannot answer yet** is whether the per-PR differences between levels are real or noise.

### 10.5 Limits of these numbers

- **The judge is unchecked.** No second judge and no human audit has been run. A judge of the reviewer's own model may match too generously or too strictly; the 48% to 56% precision has not been inspected by hand.
- **One review per cell.** Replies on this server are not exactly repeatable (Section 5), and nothing here measures run-to-run variation.
- **Retried cells are a different kind of reply.** 233 usable cells came from a retry. They cover 5.2% of their references, against 6.5% for the others. That is not a like-for-like comparison, since the retried cells are the PRs the model struggled with.
- **`L01_min` is slightly flattered** by the 15 PRs left out of it (Section 9.5). On the 158 PRs with every level usable it still has the highest recall.
- **The first JSON object is read** when a reply contains more than one (Section 5).

## 11. Still open

1. **How well the judge agrees** with a second judge from another model family and with a human check.
2. **Whether the differences between levels within a PR are real or noise.** This needs repeated reviews of some cells.
3. **How the selector stage handles a PR with a missing level** (Section 9.5).
4. **Whether the parser should read the last JSON object in a reply instead of the first** (Section 5).

## 12. Where the files are

All of it is git-ignored and exists on the laptop and on the cloud server.

| Path under `data/aacr/outcomes/qwen3.8-27b_codex-rubric_v1/` | Content |
|---|---|
| `reviews/<PR>/<level>.json` | one file per cell: status, raw reply, parsed findings, token counts, timings (1,850 files) |
| `logs/reviews_20261114-181507.log` | the full run's log: one line per call, a progress line every five minutes |
| `logs/reviews_20261114-175742.log` | the three-PR timing run |
| `logs/reviews_20261115-005717.log`, `logs/reviews_20261115-011901.log` | the retry trial and the retry run |
| `logs/judgments_20261115-053223.log` | the judge run |
| `judgments/<PR>/<level>.json` | the judge's record for each generated comment, and which references were covered |
| `table.csv`, `table.json` | one row per (PR, level): tokens, review status, retry steps, findings, recall, precision |
| `coverage.csv` | one row per (PR, level, reference): covered or not |
| `manifest.json` | the settings, prompt and input hashes of the run, and the judge's identity |

The folder is about 30 MB. The times in the log are the server's clock (China Standard Time).
