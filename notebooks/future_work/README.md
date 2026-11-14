# Future work: the outcome table with Claude as the reviewer

`claude_outcome_table.py` builds the same (PR × level) outcome table as
[`notebooks/outcome_table.ipynb`](../outcome_table.ipynb), but the frozen reviewer is a Claude
model instead of `Qwen/Qwen3.8-27B` on a cloud GPU. It needs no GPU: a laptop with `data/aacr/`
is enough.

**Status (November 14, 2026):** the code is tested with stand-ins for the API and for Claude
Code, and against the real SDK with a mocked network, including stopping and resuming at each
stage. Through Claude Code on a Max plan, `code-check` passed, the first PR was reviewed and
judged end to end, and the full review run was started and then stopped on purpose with **183 of
the 1,569 reviews saved** (22 PRs complete; see "First run" and "The full run so far" below).
Only the first PR is judged. **That is a test of the pipeline, not a result**, and nothing has
been sent through the API.
The project's reviewer is still Qwen3.8-27B. The roadmap (Section 8.5.2) allows a switch to
Claude only if Qwen fails criteria fixed before a pilot, and this script does not change that
decision. It makes the Claude table cheap to produce when it is wanted.

## Three ways to reach the model

The same script, prompt, parser and judge metric in each case. `--via` picks the way; the two
ways write to different run folders and never share a reply.

| | `--via api` with a Max plan's included credits | `--via claude-code` on the plan's sign-in | `--via api` with bought credits |
|---|---|---|---|
| What it costs beyond the plan | Nothing, up to the monthly credit ($100 on Max, $200 on Ultra) | Nothing | Per token |
| What limits it | The month's credit. When it is used up, requests stop until the next credit arrives; unused credit does not roll over | The plan's usage limits (per 5 hours and per week), shared with your own use of Claude | Your balance |
| Batch discount (half price) | Yes | No | Yes |
| Set-up | On claude.ai: Settings > Billing > API credits > Link organization. Then `ant auth login` or an API key from that organization | `code-login` once, then `code-check` | Buy credits in the Claude Console |
| What the model receives | Exactly the rubric as the system prompt and the request as the user message | The same, plus about 560 tokens that Claude Code adds to every request (listed below). Its own prompt is replaced and every tool is removed | As in the first column |

Sources, read on November 13, 2026: Anthropic's help articles
[Monthly API credits for Max, Ultra, and Team plans](https://support.claude.com/en/articles/17154008)
and [article 15036540](https://support.claude.com/en/articles/15036540), which says that with a
plan sign-in the Agent SDK and `claude -p` draw from the plan's limits.

**Using a plan sign-in for a bulk job is your call.** Claude Code's print mode (`claude -p`) is a
documented feature and runs on a plan sign-in. Anthropic's
[consumer terms](https://www.anthropic.com/legal/consumer-terms) (Section 3) restrict access by
script except through an API key "or where we otherwise explicitly permit it", and the
[Claude Code usage policy](https://code.claude.com/docs/en/legal-and-compliance.md) says plan
limits "assume ordinary, individual usage". Neither page says whether a run of about 1,500
scripted reviews counts as ordinary use. The included API credits are the route Anthropic
describes for this kind of work.

### Why not a chat window, or agents started from one

| | A chat or Claude Code window doing the reviews itself | This script, either way |
|---|---|---|
| What the model sees | The window's own system prompt, and tools that can open any file on the machine, including the other levels and AACR's reference comments | The pinned Codex rubric and one context level, with no tools. `--via claude-code` adds a few fixed sentences of Claude Code's own (listed below) |
| Settings | Model and effort are session settings, not recorded per review | `claude-opus-5-5`, effort `medium`, recorded in every review file |
| Resuming and rerunning | Manual | Each reply is saved once; re-running sends only what is missing |

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
| Reply format | Not measured yet | About one reply in eight is a complete review written as a Markdown list instead of the rubric's JSON. This script reads those too (see "Reviews written as Markdown") |

Not used, and why:

- **Structured outputs** (schema-enforced JSON): the rubric already specifies the JSON, and leaving it off keeps the request identical to the Qwen reviewer's. The price is the Markdown replies above, which are converted after the fact instead.
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

## Running it through Claude Code on a plan sign-in (no API)

The Claude desktop app's own sign-in is not shared with scripts, so sign Claude Code in once:

```bash
.venv/bin/python notebooks/future_work/claude_outcome_table.py code-login
```

Then check it. The check shows the plan, lists the text Claude Code adds to a request (inspected
on this machine, with nothing sent), and makes two tiny real calls:

```bash
.venv/bin/python notebooks/future_work/claude_outcome_table.py code-check
```

After that, add `--via claude-code` to the usual commands:

```bash
.venv/bin/python notebooks/future_work/claude_outcome_table.py review --via claude-code --first 15
```

```bash
.venv/bin/python notebooks/future_work/claude_outcome_table.py judge --via claude-code --first 15 --judge claude
```

- **The program:** the script uses `claude` on `PATH`, else the build the Claude desktop app keeps for its own Code sessions (on macOS). `--claude-bin` names another.
- **One call per review:** `claude --print` with the rubric as `--system-prompt`, `--tools ""`, `--safe-mode` (no `CLAUDE.md`, memory, skills, plugins, hooks or MCP servers), no saved session, run in one fixed empty folder. The request goes in on standard input. A judge call has no system prompt, so it passes an empty one; leaving the option out would bring in Claude Code's own agent prompt.
- **What Claude Code adds to every request** (version 2.1.321, 1,608 characters, about 560 tokens, the same for every cell):
  - a billing line and the sentence "You are a Claude agent, built on Anthropic's Claude Agent SDK." in front of the system prompt;
  - a note in front of the user message that gives the account's email address and says to treat it as private;
  - a system message after the user message with the working folder, operating system, model name and today's date.

  None of it concerns the PR, the levels or the references, and the model is given no tools. It still makes this reviewer slightly different from the API one, which is one more reason the two write separate tables. The date changes daily, so a review made on another day has a slightly different input.
- **Updates are caught:** the desktop app updates Claude Code often. Each run inspects the additions first, refuses to send anything if the model would be given tools, records the additions in `manifest.json` (email, folder and date masked), and warns if they differ from the ones the folder's earlier replies were made with.
- **An exported API key is ignored** for these calls, so they cannot be billed to the API by accident.
- **These limits are the ones your own Claude use draws on**, including a window like the one that may have started the run. `--workers` (1 by default) sets how many reviews run at once.

### Plan limits: seeing them, stopping at them, continuing after them

With every reply, Claude Code reports how much of the plan's 5-hour and weekly limits is used,
when each resets, and whether paid extra usage is in use. The script keeps that report with the
reply, prints it every ten reviews, and `code-check` and `status` show the latest one.

| Situation | What the run does |
|---|---|
| The plan's limit is reached | It stops and says when the limit resets. The same command later sends only the missing reviews |
| `--wait-on-limit` | Instead of stopping, it waits for the reset and continues by itself. A laptop that slept continues after waking |
| `--week-cap PERCENT` (default 90) | It treats that share of the weekly limit as the limit, to leave the rest for your own work. Running it again while still at the cap sends nothing |
| `--five-hour-cap PERCENT` | The same for the 5-hour limit (by default it runs to the limit) |
| `--until "YYYY-MM-DD HH:MM"` or `--until HH:MM` | It sends nothing after that time, for example shortly before the weekly limit resets, so an unattended run does not start on the new week |
| Paid extra usage is being used | It stops at once and sends nothing more. If extra usage is available on the account at all, it stops at 95% of a window so no request crosses into it |
| Three requests in a row fail for another reason | It stops with the last error. Nothing saved is lost |

An overnight run that leaves room for your own use and ends before the weekly reset looks like this:

```bash
caffeinate -i .venv/bin/python notebooks/future_work/claude_outcome_table.py review --via claude-code --workers 2 --wait-on-limit --five-hour-cap 85 --until "2026-11-14 15:30"
```

`caffeinate -i` keeps a Mac from idle-sleeping while the run is active; a closed lid still pauses it.
Measured over 160 reviews in two 5-hour windows (November 14, 2026, Max plan, with some other use
of the plan at the same time, and limits reported in whole percent):

| Per 1M input tokens | Share of the limit used |
|---|---|
| 5-hour limit | 7 to 9 points |
| Weekly limit | 0.6 to 0.9 points |

- **One 5-hour window is about a tenth of the table.** Capped at 85%, it carries 10M to 13M input tokens of the table's roughly 100M.
- **The run works in short bursts.** Two requests at a time use up a window's 85% in about 25 minutes. With `--wait-on-limit` the run then waits about four and a half hours for the reset, so most of the time it is waiting, not working.
- **The whole table's reviews take roughly 60% to 90% of one weekly limit,** and ten or so 5-hour windows, which is about two days of wall time.
- **The judge step comes on top:** about 10% to 20% more, going by the one PR judged so far.
- **Long inputs:** 51 of the 1,569 requests are above 200,000 tokens by the dataset's count and need the 1M-token window. On the Max plan tested, it is there (see "First run"). Where it is not, those requests are recorded under `api/rejected/` and their cells show `error`.
- **Prompt caching is switched off** for these calls (`DISABLE_PROMPT_CACHING=1`). Claude Code otherwise writes every request to its one-hour cache, which nothing here reads and which counts as twice the input at API rates.

### First run (November 13, 2026, Max plan, Claude Code 2.1.321)

`review --via claude-code --first 1`: the 8 requests of the first PR (FreeCAD, C++), one at a time.

| What | Measured |
|---|---|
| Outcome | 8 of 8 answered by `claude-opus-5-5`, none rejected, refused or cut off; all 10 cells `ok` (two levels repeat another's input) |
| Reply format | Valid review JSON every time, 4 or 5 findings each. The model wraps the JSON in a heading and a code fence, which the parser accepts |
| Time | 5 minutes in all, 37 seconds per review on average |
| Input tokens | 1,542,103 by Claude's count against 942,448 by the dataset's count: 1.64 times as many. If that holds, the full table is about 100M input tokens, not 62M |
| Longest input | 420,841 tokens (255,992 by the dataset's count), accepted: the plan's Claude Code has the 1M-token window |
| Output tokens | 30,854, about 3,900 per review, of which about 60% is thinking |
| Not measured | The share of the plan's usage limits this took (see Settings > Usage on claude.ai). These 8 reviews were made with prompt caching still on |

At 37 seconds each, the full table's 1,569 reviews take about 16 hours one at a time, if the plan's limits allow.

`judge --via claude-code --first 1 --judge claude --workers 3` then scored those reviews: 37 judge
calls (identical prompts are asked once), 6 seconds each, 53,527 input and 13,233 output tokens,
no errors, and a `table.csv` of 10 rows. About 40% of the judge's input is the text Claude Code
adds, because a judge prompt is short.

The PR has 19 reference comments. Each level's review covered 3 or 4 of them (recall 0.16 to
0.21) with 4 or 5 findings. The levels differ by one finding at most, and the reviewer is not
repeatable, so this one PR says nothing about which level is better. The judge here is the same
model as the reviewer, which the roadmap asks to avoid or to check against another judge.

### The full run so far (November 14, 2026)

`review --via claude-code --workers 2 --wait-on-limit --five-hour-cap 85 --until "2026-11-14 15:30"`
ran for two 5-hour windows and was then stopped with Ctrl+C. The same command continues it.

| What | Measured |
|---|---|
| Reviews saved | 183 of 1,569, in 22 complete PRs and part of a 23rd. None rejected, refused, failed or cut off |
| Input tokens | 14.47M by Claude's count against 8.61M by the dataset's count: 1.68 times as many. About 86M are left |
| Output tokens | 319k, about 1,700 per review |
| Time | 20 seconds per review on average, 94 at most |
| Findings | 275 in all. 53 reviews (29%) report none |
| Reply format | 159 replies hold the rubric's JSON and 24 (13%) are a Markdown list |

### Reviews written as Markdown

The rubric asks for one JSON object. About one reply in eight is instead a complete review
written for a reader: a `**Findings**` heading, one `**[P2] title**` item per finding with its
file, lines and confidence, then `**Overall verdict: patch is incorrect**`. Nothing is missing
from such a reply and no text was lost on the way (the visible output tokens match the saved
text). Its share does not depend on the input's size or on the hour.

The shared parser reads JSON only, so it would mark such a reply `malformed` and the table would
score it as an empty review. That would make a level look bad for a reason that has nothing to do
with its context. Asking again would spend the same input a second time. So when the review
records are written, `markdown_review` reads the list into the rubric's JSON shape:

- **It asks no model.** It is a fixed set of patterns, tested in `tests/test_claude_outcome_table.py`.
- **It guesses nothing.** A reply whose layout it does not know stays `malformed`, and findings without the rubric's priority tag are never read as "no findings".
- **The saved reply is not changed.** `api/replies/` keeps the model's own text. The review record holds the JSON form in `reply.text`, the model's text in `reply.original_text`, and `reply.converted`.
- **A bare file name is completed.** AACR's judge compares file paths exactly, so `Tools.cpp` is replaced by `src/App/Tools.cpp` when exactly one file of the PR ends that way. The name as written is kept in `path_as_written`.
- **It is part of the reviewer's identity** (`markdown_replies: markdown-findings-v1` in each record), so records made without it are rebuilt, at no cost.

On the 24 Markdown replies so far it read all 24: 59 findings, each with a file of the PR and a
line range, 49 with a stated confidence, and every reply with its verdict. Each step prints how
many replies were read this way and how many could not be read, and the manifest records both.

## Seeing results while a run is going

A reply is saved under `api/replies/` the moment it arrives, but that folder is named by request
hashes and is not meant for reading. The readable files (`reviews/<PR>/<level>.json` and
`table.csv`) are written when a step ends, which for a long run with waits can be hours away.
To write them from whatever is saved so far:

```bash
.venv/bin/python notebooks/future_work/claude_outcome_table.py table --via claude-code
```

- **It sends nothing and costs nothing,** and it is safe while a review run is going.
- **A cell that is not answered yet shows `error`** in `review_status`, with "no saved Claude reply for this request yet" in its file. It is not a failure, and the next `review` fills it.
- **Score columns are filled only for judged reviews.** `judge` is a separate step that uses the plan's limits again.

## Stopping and resuming

Every step can be stopped at any point (Ctrl+C, a closed laptop, a crash) and run again with the
same command. **A request that was answered or submitted is not sent again**, so continuing never
pays for a review twice. What makes that hold:

| Situation | What happens |
|---|---|
| A reply has arrived | It is saved at once under `api/replies/`, named by a hash of the request. Every later step and run reads it from there. The first reply to a request is kept; nothing overwrites it |
| A batch is submitted and the run stops | The batch is recorded under `api/batches/` and keeps running at Anthropic. The next run collects it and sends only requests that are in no batch |
| The run stops while collecting a batch | The batch counts as collected only after all its results are saved, so the next run reads it again (reading results is free) |
| An upload is cut off, or its response is lost | A marker file is written before each upload. The next run looks the batch up on the account: if it exists it is taken over, and only if the account has no such batch are its requests sent |
| Ctrl+C in `--mode direct` | No new request is sent, and the run waits for the ones already sent so their replies are saved |
| Two runs started in the same run folder | The second one stops immediately (a lock file, on macOS and Linux), instead of sending the same requests |
| Some reviews were cut off at `--max-tokens` | Running `review` again with a higher `--max-tokens` sends only those. The complete reviews are kept |
| A request failed, expired or was canceled | The API does not bill it. It stays pending and is sent on the next run |
| The API credit runs out (for example a plan's monthly credit) | The run stops and says so. Requests that were not processed are not billed and not marked as bad; the next run sends only those |
| The plan's usage limit is reached (`--via claude-code`) | The run stops and says so. The next run, after the limit resets, sends only the missing reviews |
| The pilot ran before the full table | The pilot's replies are reused, because replies are saved by request and not by run |
| `api/replies/` was deleted | `collect --again` reads every recorded batch's results again, free of charge, for 29 days after the batch was created |

What does lead to new, paid requests, each shown with its count and cost before you confirm:

- **A different reviewer or prompt:** another `--model`, `--effort`, `--replicate` or `--via`, or a changed rubric. These are different reviews by design, and by default they go to another run folder.
- **A rebuilt `context_levels.jsonl`:** cells whose text changed need a new review. Unchanged cells are reused. `plan` and `review` print a warning when the file differs from the one the folder's replies were made with.

Limits to know:

- **Collect within 29 days.** Anthropic keeps a batch's results for 29 days after its creation. `status` lists batches that are not collected yet.
- **Direct mode is less safe than batch mode.** If the network drops while a direct request is running, that request may have been billed without its reply arriving, and it is sent again. This is limited to the requests in flight (`--workers`, 4 by default). Batch mode has no such case.
- **Do not delete the run folder** while batches are open. If it is lost anyway, `status --account` lists the account's batches and `collect --adopt <batch id>` reads one back.
- **A hard ceiling:** set a spend limit for the workspace in the Claude Console. The script's estimates cover input tokens, and output is only known after a pilot.

## Running it through the API

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
- **Batch mode is the default.** `review` uploads the requests, waits and collects. Ctrl+C stops the waiting, not the batches; running the same command again collects them and sends nothing twice. `--no-wait` submits and exits.
- **`--mode direct`** sends the requests now at standard prices, 4 at a time. It is subject to the account's per-minute token limits, which the largest levels can exceed on a new account.
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

Under `data/aacr/outcomes/<model>_<effort>_codex-rubric_v1/`, with `claude-code_` in front for
`--via claude-code` (ignored by git, like all of `data/aacr/`):

| Path | Content |
|---|---|
| `reviews/`, `judgments/`, `table.csv`, `table.json`, `coverage.csv`, `manifest.json` | The same files as the main pipeline (see [`src/review_outcomes/README.md`](../../src/review_outcomes/README.md)). The manifest also records the model the API reported, the dates of the first and last reply, token totals and batch IDs, because API models are retired eventually |
| `api/replies/` | One file per answered request: the model's own text, token counts, stop reason, served model, message and batch IDs. It fills during a run. This folder is what was paid for; keep it. The folder is called `api` for both ways |
| `api/rejected/` | Requests the API rejected as invalid, for example an input beyond the model's window. They are not sent again unless `--retry-rejected` is passed, and their cells show `error` in the table |
| `api/batches/` | One file per submitted batch. A `submitting-*.json` file marks an upload that was interrupted; the next `review`, `judge` or `collect` settles it by checking the account |
| `api/token_counts/` | Exact input token counts from `count` |
| `claude_outcome_table.log` | Everything the steps printed |

Tests, with no API calls:

```bash
.venv/bin/python -m unittest tests.test_claude_outcome_table -v
```
