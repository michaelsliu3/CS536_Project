# The project's target: a real harness's review quality at far fewer tokens (November 21, 2026)

**Status: set on November 21, 2026, for now.** The learned context selector should produce reviews of similar quality to a real coding agent's on the same hunks, while using far fewer tokens. A selector that does not improve quality is acceptable. The real agent is Codex CLI (open source) on the same model as our reviewer. How "similar" is measured is proposed below and not yet agreed in detail.

This adds to the target used so far, which compared the selector only with our own fixed context levels ([per_hunk_review_test.md](per_hunk_review_test.md), Section 20.1). That comparison stays, as a second target (Section 3 below).

**No agent session has run on the real model in this design yet.** The code was rehearsed end to end against a stand-in server (Section 4). The only real session so far is one per-hunk session under earlier settings (Section 3).

## 1. Why a real harness, and how it is used

A coding agent gathers its own context before it reviews: it runs read-only commands in the repository, reads files and searches for definitions. That is what people would otherwise use. Matching its review quality at a fraction of its cost is worth reporting even if the selector never beats the best fixed level.

Codex is a comparison only. In both runs below we send it the pull request's text and the change, and it gathers its own context. Nothing it reads is given to our reviewer, no context level is built from it, and the selector is not trained to choose it. An earlier design recorded what Codex read and had our reviewer review it as one more level (`HC1_codex`); that was dropped on November 21.

Codex runs in two ways:

| Run | What Codex gets | What it is for |
|---|---|---|
| **Per hunk** (`C01_codex_hunk`), the comparison | Our reviewer's own request for one hunk (the pull request's title and description and the hunk's diff), with leave to look around the repository first. It answers in our reviewer's format | The like-for-like comparison. The unit, the request, the reply format, the parser and the judge are our reviewer's. The one thing that differs is that an agent gathers the context |
| **Per pull request** (`C02_codex_pr`), as a developer would run it | Codex's own review command: its standard request to review the changes against the base branch, plus the pull request's title and description. No diff: it reads the diff itself | What the tool gives when used as shipped. Reported next to the comparison |

### Why the per-hunk run is the comparison

The unit of review decides most of the recall, whatever the context:

- In the project's first run, one review call per pull request covered **6 to 7%** of the reference comments at every context level. It used Codex's review rubric, and the reviewer wrote one finding or none in 84% of its reviews ([outcome_table_first_run.md](outcome_table_first_run.md)).
- With one call per hunk, the same model covered **47 to 63%** on the first five pull requests ([per_hunk_review_test.md](per_hunk_review_test.md), Section 12).

Codex's review command uses that same rubric and reviews a pull request in one go. So its per-PR recall is expected to be low for the same reason (not measured yet). Setting it against our per-hunk reviewer would mostly measure the unit of review, not the context, and the selector would look good for the wrong reason.

## 2. How it is measured (proposed)

| Part | Measure |
|---|---|
| Hunks | The hunks our reviewer reviews in the run being compared, each reviewed by Codex once |
| Tokens, selector | The reviewer's input at the level the selector chose, per hunk |
| Tokens, Codex | Everything its session sent to the model, over all its requests, and what the model wrote. Each request resends the conversation so far, so text read early is counted again by every later request. The part the server had cached is reported separately |
| Quality | Recall of the reference comments, scored by the same judge, and the severity-weighted score ([review_score_design.md](review_score_design.md)). A PR's hunk reviews are put together as in our own tables |
| "Similar" | Recall within one point of Codex's, checked with a paired bootstrap over hunks. To be fixed before the comparison is run |
| Second number | Codex's per-PR review on the same pull requests: recall, and precision over its findings in files with a reference comment |

### What is not equal between the two sides

These are kept, and reported with the results.

| Run | Difference | Codex | Our reviewer |
|---|---|---|---|
| Per hunk | How context arrives | An agent loop: it runs commands and reads their output over several requests | One call with the level's text |
| Per hunk | Thinking | On (the server's default for a request that does not turn it off) | Off |
| Per hunk | Instructions | Codex's general agent instructions, then our request | Our request only |
| Per hunk | A reply that is not in the asked format | Counted as no findings, and reported as a count | Gets the retry rule |
| Per PR | Unit | One session reviews the whole pull request | One call per hunk |
| Per PR | Instructions | Codex's review rubric, which asks only for issues the author would fix | The AACR-Bench paper's per-hunk prompt |
| Per PR | Hunks reviewed | All of the pull request | In the pilot runs, only hunks in a file that has a reference comment |

The last row does not change recall: a finding can only cover a reference in its own file. It does change precision, so the per-PR precision is also reported over findings in files with a reference comment (`generated_in_reference_files` in the table).

### Settings changed from Codex's defaults

| Setting | Value | Why |
|---|---|---|
| Model and server | Qwen3.8-27B on our vLLM server, through the Responses API | The same model as our reviewer |
| Sandbox | Read-only, no network, no web search, never asks for approval | A review must not change anything or reach outside |
| Command output shown to the model | 10,000 tokens per command | Codex gives every model in its own list this much, but only 10,000 bytes to a model it does not know. With the default it cut the middle out of a 4,334-token diff |
| Shell | A plain shell, with the real `git` first on the path (macOS only) | In the sandbox, the macOS `git` launcher printed several warning lines into every git command's output |

## 3. Two cautions

**The token half is met by any fixed level, so the claim rests on the quality half.** Our reviewer's input per hunk, on the 432 hunks of the 30 ContextCRBench pilot pull requests:

| Level | Per hunk, mean | Per pull request, mean | Median | Largest |
|---|---|---|---|---|
| `H01_min` (no context) | 786 | 11,326 | 3,681 | 112,573 |
| `H02_function` | 1,266 | 18,235 | 6,371 | 117,664 |
| `H03_file` | 2,160 | 31,116 | 9,297 | 172,984 |
| `H04_callees1` | 2,870 | 41,328 | 15,649 | 211,001 |

The one real Codex session so far reviewed a single hunk (jQuery pull request 3956, hunk 3, before the output setting above): 153,318 input tokens over 8 requests, 131,456 of them cached, and 16,076 output tokens, 13,569 of them thinking. It took 11 minutes. That is one session; the run gives a real average. At that rate the 108 pilot hunks are about 20 hours of sessions, or a little over 3 hours with 6 running at a time.

**The selector still has to beat simple rules.** If giving every hunk the file also matches Codex, the target is met without the selector doing anything. So the selector is also compared with the fixed levels, with random choice and with the cheapest-first rule at equal quality, as in [per_hunk_review_test.md](per_hunk_review_test.md), Section 20. It should save tokens beyond what a simple rule saves.

## 4. What exists

- **Code:** `scripts/harness/record_codex.py` and `src/harness_context/codex.py`, on the branch `contextcrbench-pilot` (pull request 9). The scripts table in `scripts/README.md` describes the commands: `check`, `checkout`, `hunks` (the comparison), `review` (per pull request) and `score`.
- **How a hunk session runs:** Codex CLI 0.171.0 runs `codex exec` on a checkout of the pull request at the reviewed commit, with our reviewer's request for the hunk after a short preface that lets it explore first. Its final message is the review, in our reviewer's tag format.
- **How a per-PR session runs:** `codex exec review` on the same checkout. Its request is the one Codex itself builds for a review against a base branch, word for word, followed by the pull request's title and description. The review comes back as JSON in the format of Codex's review rubric, which is the rubric and parser of the project's first run.
- **What is kept:** each session's folder holds Codex's event stream and its session files, which have every message, command, command output and the token counts of each request.
- **How it is scored:** `score` parses Codex's own reviews, judges them with the profile's judge, and writes one row per pull request and per hunk. `comparison.json` sets Codex next to the four levels on exactly the hunks Codex reviewed, using the levels' saved reviews and judgments.
- **Server setting:** vLLM must be started with tool calling on: `--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder`. With thinking off, these flags do not change our reviewer's replies.
- **Rehearsed without a GPU:** `scripts/harness/fake_model_server.py` answers Codex with a scripted command and a made-up review. With it, the real Codex program ran a hunk session and per-PR sessions on pilot pull requests, and the saved sessions went through parsing, judging (with a made-up judge) and the tables. This checks the plumbing. It says nothing about review quality or real cost.

## 5. What it needs

- **Hunk sessions on the real model.** First a few, to see how long a session takes and what it costs under the current settings. Then the pilot's 108 hunks.
- **Our reviewer's results on the same hunks**, at the four kept levels (`H01_min`, `H02_function`, `H03_file`, `H04_callees1`). The pilot has `H01_min` and `H03_file` so far.
- **Per-PR sessions on the same pull requests**, for the second number.
- **A check on what Codex reads.** Read-only still lets it read any file on the computer it runs on, and what it reads goes to our model server. Each session's summary counts the commands that named a path outside the checkout.
