# Per-hunk review test (branch `per-hunk-review`, started November 15, 2026)

**Status: the code is written and tested, and a first test on five PRs was reviewed and judged on November 15 (Section 12). On those five PRs the first three levels reach 47 to 63% recall, against 5 to 16% for the per-PR run. From `H04_pr_diff` on, the reviewer mostly commented on the other hunks it was shown. The levels and the scoring were changed for that (Section 13) and the five PRs were reviewed again later that day (Section 14): the fall is gone, every level now reaches 42 to 58% recall at 13 to 17% precision, and the levels cannot be told apart at this size, because the same input run twice differs by about as much as the levels do. With the reviewer's thinking on, two levels covered 24 and 27 of 38 references at 10 to 12% precision, for about ten times the output and 8.5 times the review time (Section 15). Nothing here changes the per-PR pipeline, its data or its table. The work is on the branch `per-hunk-review` and is not merged.**

This document records why the test exists, what was decided and when, what was built, what was checked, and what is still open.

## 1. Why this test

The first outcome table ([outcome_table_first_run.md](outcome_table_first_run.md)) has about 6 to 7% recall at every context level. The reviewer writes one finding or none in 84% of its reviews, and it comments in 19% of the files that hold reference comments (Section 11.1 there).

Two causes could explain that, and the first table cannot separate them:

- **The bar in the prompt.** The Codex rubric tells the reviewer to prefer no finding over a doubtful one.
- **One call for the whole PR.** Given every changed block at once, the reviewer picks one issue and stops.

The AACR-Bench paper's rows for plain LLMs change both at once: a prompt that asks for every issue, and one diff hunk per call. They reach 24.25% to 47.24% recall at 5.10% to 11.30% precision (Section 11.2 there).

**Decided on November 15, 2026:** test both changes together, on a branch.

1. The reviewer's prompt is the paper's prompt, word for word, and is sent once per diff hunk. The reviewer answers in the paper's tag format (decided later the same day; the first version of this test used our own wording and a JSON reply).
2. The context levels are rebuilt so that each one starts from that one hunk.

## 2. Timeline

| Date | What happened |
|---|---|
| November 14 | First outcome table. Three checks afterwards: the reviewer comments on few files, the paper prints its prompt for plain LLMs, AACR-Bench has a v2 reference set. |
| November 15 | The decision above. Branch `per-hunk-review` created from master at `6112480`. Tracer, levels, prompt and outcome-table code written. Five PRs built per hunk on the laptop as a check. |
| November 15, later | The first prompt was our own wording of the paper's instructions, with a JSON reply. It was replaced by the paper's exact text and tag format, with a parser for the tags. |
| November 15, later | Decided that the per-hunk test keeps a data folder of its own, `data/aacr_hunk/`, and the original context levels stay as they are. A guard was added so a per-hunk build can never write into the per-PR file. 209 tests pass, 23 of them new. |
| November 15, later | The branch was pushed. The five-PR build was reviewed and judged on the cloud server (Section 12). |
| November 15, later | After that test: the other hunks were taken out of the chain, context blocks name the lines the PR changes in them, a summary level was added at the end, and only findings that quote the hunk under review count (Section 13). The five PRs were rebuilt. |
| November 15, later | Second test: the five PRs reviewed and judged again with the changed levels and counting rule (Section 14). The cloud server's clock is 13 hours ahead and showed November 16. |
| November 15, later | Two levels run again with the reviewer's thinking on (Section 15). A look at why the levels do not differ and at what recall and precision measure (Section 16). Pull request 7 opened for this branch. |
| Not yet | A review run, a judge run, and the comparison with the per-PR table. |

## 3. What one review covers

- **A hunk** is one `@@` block of the diff the reviewer sees. That diff has three unchanged lines around each change, as GitHub shows it. The 185 built PRs have 4,136 hunks: 22.4 per PR on average, median 10.
- **A cell** is one (hunk, level) pair. A hunk's id is its PR's id and its position in the diff, for example `gofr-dev__gofr__1617__h0004`.
- **Files without a hunk** have no cell: binary files, pure renames and mode changes.

The data set's `stats.hunks` field counts something else (runs of changed lines with no surrounding lines) and must not be used for this.

## 4. The levels, per hunk

The eleven levels are in `configs/levels_hunk/`. `H01_min` to `H10_all` mirror the ten per-PR levels `L01_min` to `L10_all` one to one. `H11_pr_summary` is one extra level at the end.

| Level | What it adds |
|---|---|
| `H01_min` | The PR's title and description, and this hunk. This is the paper's setting without code context. |
| `H02_function` | The full function or class this hunk changes |
| `H03_file` | The file this hunk is in (windows around the hunk for files over 400 lines) |
| `H04_callees1` | Definitions of what this hunk's code calls directly |
| `H05_callers1` | Direct callers and users of this hunk's code |
| `H06_tests1` | Tests that directly exercise this hunk's code |
| `H07_agent` | Repository guide files, and text-search hits for the names this hunk changes |
| `H08_depth2` | Callees and callers two hops from this hunk |
| `H09_depth2_docs` | Documentation sections and tests up to two hops away |
| `H10_all` | Everything the tracer finds from this hunk, up to three hops, with no item limits |
| `H11_pr_summary` | A list of the PR's other changes, with no code (Section 4.3) |

Item limits, the 256,000-token input cap, the tokenizer and the rendering are those of the per-PR levels.

**The first test used another order** (Section 12). Its level 4 was `H04_pr_diff`, which showed the PR's other hunks as a diff, and every later level contained it: `H05_callees1`, `H06_callers1`, `H07_tests1`, `H08_agent`, `H09_depth2`, `H10_depth2_docs`, `H11_all`. Section 13 records the change. Section 12 uses the first test's names.

### 4.1 Which changed symbols belong to a hunk

A hunk's changed symbols are those that overlap its changed lines.

The tracing tool has a second rule, which the per-hunk levels keep. When no change in a file touches a symbol (only an import line changed, for example), the tool takes every symbol of that file as changed and marks it `whole_file`. A hunk of such a file gets all of them.

This rule also shapes the per-PR levels and was not written down before: `L02_function` shows every function of a file whose only change is outside all of them. It was found here because 43 of one PR's 54 changed symbols had this mark.

### 4.2 How the trace is repeated from one hunk

The tool walks from all of a PR's changed symbols at once and reports each caller's nearest changed symbol only. For this test the trace also keeps every link among the traced symbols (`call_edges`, `caller_edges`). The walk is then repeated from one hunk's symbols over those links, so each hunk gets its own callers, callees and hop counts. A PR is still traced once.

- **Another changed function can be a hunk's caller or callee.** In the per-PR levels it is only ever shown as changed code.
- **Text-search hits** are kept for the names of the hunk's own symbols.
- **Check built in:** every build compares a walk from all changed symbols over the kept links with the tool's own walk, and warns on any difference.

### 4.3 The PR's other changes

A per-PR review sees the whole diff. A per-hunk review sees one hunk. The levels give the rest of the PR back in two ways, neither of which shows another hunk's diff lines.

**The context is the code after the change.** Every context block is read from the PR's reviewed version. A change in another hunk therefore reaches the reviewer whenever the tracer links it to this hunk: the same function from `H02_function`, the same file from `H03_file`, a called or calling function from the callee and caller levels, a test from the tests level.

**Blocks name what else the PR changes in them** (`input.mark_changed`, on from `H02_function`). The code after the change does not show which lines are new. So a block's header names the lines the PR changes in it outside the hunk under review, for example:

```
### changed file: app.py  [lines 1-40]  (this pull request also changes lines 14, 22-25)
```

**`H11_pr_summary` lists the other changes** (`input.other_hunks` with `input.other_hunks_form: summary`). The section is named "Other changes in this pull request" and sits between the hunk and the repository context. It has one line per file, with each other hunk's lines after the change and the enclosing code named in its `@@` header, and no code:

```
- `app.py`: lines 12-14 (def second(x):)
- `util.py`: lines 1-3
```

- **Order and room:** nearest first (the same file by distance, then the other files in diff order), up to 4,000 tokens. When hunks are left out, the section says how many are listed.
- **What this level is for:** a change with no code link to the hunk (a config file, a document, a file in another language) reaches no other level. If this level covers references the others miss, PR-wide information matters and deserves a better design.
- **The diff form still exists** (`other_hunks_form: diff`, the default of the key) but no level uses it. The first test showed why (Section 12.3).

### 4.4 What the per-PR pipeline keeps

- **A data folder of its own.** Everything of this test lives in `data/aacr_hunk/`: its copy of the AACR file, its context levels, its logs and its outcome runs. `data/aacr/`, with the per-PR levels and the first run, is not written to.
- **A guard.** A build refuses a folder whose context file holds the other unit's records. So `--unit hunk` pointed at `data/aacr` stops before writing anything, and the original `data/aacr/context_levels.jsonl` cannot be replaced by mistake. On November 15 that file still had the hash the first run recorded (`b06dee5e…`).
- **Configs:** the four new keys (`input.other_hunks`, `input.other_hunks_form`, `input.mark_changed`, `packing.other_hunks_max_tokens`) are optional. A config without them is unchanged, and so is its hash.
- **Trace:** the extra links are collected only when a per-hunk build asks for them.

## 5. The reviewer prompt

The prompt is the one the AACR-Bench paper uses for its non-agent rows ([arXiv 2601.19494v3](https://arxiv.org/html/2601.19494v3), appendix [Figure 16](https://arxiv.org/html/2601.19494v3#A3.F16) and [Figure 17](https://arxiv.org/html/2601.19494v3#A3.F17)), copied word for word. The paper is published under CC BY 4.0, which allows this with attribution.

| File in `configs/review_prompts/` | The paper's | Used for |
|---|---|---|
| `aacr_nonagent_no_context.md` | Figure 16 | An input with nothing but the PR text and the hunk: every `H01_min` cell |
| `aacr_nonagent_with_context.md` | Figure 17 | An input that shows more: the other levels |

Both files are pinned by SHA-256 and the code refuses to run if one changes. The prompt's version is `aacr-nonagent-2601.19494v3-fig16-fig17`. It is one user message with no system message, and it adds 397 tokens to a cell's input.

**How the files were made.** The text was read from the paper's HTML page on November 15. That page shows backticks and apostrophes as typographic quotes; they were put back. Nothing else was changed. The two files have the lengths of the two figures (1,881 and 1,923 characters) and differ only by the context section.

**What goes into the paper's four slots:**

| Slot | What we put there |
|---|---|
| `pr_title`, `pr_description` | The PR's title and description. An empty description is `(no description)`. |
| `diff_hunk` | The file's diff header and the hunk |
| `contexts` | Everything else the level shows: the PR's other hunks under a `###` heading, then the repository context blocks |

Each level records where its parts sit in its text, so the slots are filled by position and never by searching the text.

**The reply is in the paper's tags.** A comment quotes the lines it is about (`<diff>`), names the side (`<side>`), and gives the comment (`<note>`). `<notesplit />` separates comments and `<end />` ends the reply.

- **Parser** (`parse.parse_tagged_review`, version `aacr-tags-v1`): each `<note>` is one finding, with the quote and side written before it. A reply with a note or the end tag is readable. A reply with neither is `malformed`.
- **File:** a finding is in the hunk's file. The paper does the same, since a comment quotes the hunk.
- **Lines:** the quoted lines are looked up in the hunk. A quote that is not found leaves the finding without line numbers (`located` is false). The judge ignores line numbers.
- **No priority and no confidence:** the paper's format has neither, so these findings cannot be filtered by them afterwards.

**The retry rule is unchanged, with one new message.** A reply cut off at the output limit is sent again with a larger limit. A reply that finished with neither a note nor the end tag gets one follow-up message, which asks for the instructions' format and says to output only the end tag when there is nothing to report.

**Two known limits of using the text unchanged:**

- **The code fence is fixed.** The paper wraps the hunk in a three-backtick fence. A hunk that itself contains such a line (a change to a Markdown file) ends the fence early. This is the case for 1 of the 78 hunks in the five-PR check.
- **The hunk's lines carry no line numbers,** as in the paper.

## 6. Scoring

- **Only findings on the hunk under review count** (since the first test, Section 13). The paper's format makes the reviewer quote the code each finding is about. A finding counts when its quote is found in the hunk it was asked to review. The others, which quote other code the reviewer was shown or quote nothing, are kept in the review file under `off_hunk_findings` and are not judged. `hunk_table.csv` has their number per hunk (`findings_off_hunk`).
- **Judge:** unchanged. Each of a hunk's counted findings (the text of its `<note>`) is compared with the PR's reference comments on the same file.
- **PR rows** (`table.csv`): one per (PR, level). The PR's hunk reviews at that level are put together in diff order and scored as one review by AACR's rule, so these rows use the same metric as the per-PR table. A row is `ok` only when every hunk of the PR has a readable reply.
- **Hunk rows** (`hunk_table.csv`): one per (hunk, level), with the hunk's own findings and the references that sit on its lines. These are what a selector that picks a level per hunk would learn from.

## 7. What was checked on November 15

- **Tests:** 209 pass. The 23 new ones cover splitting a diff, repeating a trace from one hunk, the other-changes section, the per-hunk data set record, the paper's prompt and its slots, the tag parser, the follow-up message, the per-hunk outcome tables, and the guard between the two kinds of build.
- **Per-PR pipeline untouched:** the ten per-PR configs have the hashes stored in the data set, and 199 of 199 sampled saved reviews keep their cache key on this branch.
- **Five PRs built per hunk** (`data/aacr_hunk/`, git-ignored), in four languages:

| PR | Language | Hunks | Hunks with a changed symbol | Symbols in no hunk | Walk equals the tool's |
|---|---|---|---|---|---|
| `gofr-dev__gofr__1617` | Go | 30 | 29 | 0 | yes |
| `timescale__timescaledb__7915` | C | 19 | 14 | 0 | yes |
| `browser-use__browser-use__857` | Python | 17 | 14 | 0 | yes |
| `CherryHQ__cherry-studio__5637` | TypeScript | 11 | 9 | 0 | yes |
| `cline__cline__5177` | TypeScript | 1 | 1 | 0 | yes |

No level lost a line its parent shows, and none reached the input cap.

**Input size per level, over the 78 hunks:**

| Level | Median tokens per hunk | Hunks whose input differs from the level before |
|---|---|---|
| `H01_min` | 352 | 78 |
| `H02_function` | 785 | 67 |
| `H03_file` | 1,521 | 78 |
| `H04_pr_diff` | 8,370 | 77 |
| `H05_callees1` | 8,750 | 49 |
| `H06_callers1` | 9,279 | 29 |
| `H07_tests1` | 9,357 | 20 |
| `H08_agent` | 14,279 | 78 |
| `H09_depth2` | 14,748 | 69 |
| `H10_depth2_docs` | 14,975 | 16 |
| `H11_all` | 16,066 | 40 |

A level identical to the one before is reviewed once, as in the per-PR run.

## 8. What a run would cost

Measured on the five PRs, with the review script's dry run:

| | Per PR (ten levels) | Per hunk (eleven levels) |
|---|---|---|
| Cells | 50 | 858 |
| Calls, after identical inputs are merged | at most 50 | 601 |
| Input tokens | 1.42 million | 5.25 million |

- **For all 185 PRs:** about 32,000 calls and about 250 million input tokens. This is an estimate scaled from five small and medium PRs, so it is rough.
- **Against the first run:** that run made 1,569 calls with 59.5 million input tokens in 5h48m. A full per-hunk run is about four times its input, so about a day of GPU time, plus a longer judge run because there will be more findings.
- **A pilot comes first:** a subset of PRs and levels, to see recall and precision before paying for the full run.

## 9. How to run it

Build the per-hunk levels (laptop or server; no GPU). The folder must be its own; `data/aacr` is refused:

```bash
.venv/bin/python scripts/dataset/build_dataset.py data/aacr_hunk --unit hunk
```

See what a review run would send, without a server:

```bash
OUTCOME_PROFILE=cloud-hunk .venv/bin/python scripts/outcome/run_reviews.py --dry-run
```

Review and judge the first PRs of the context file with the `cloud-hunk` profile of `notebooks/outcome_table.ipynb`:

```bash
OUTCOME_PROFILE=cloud-hunk .venv/bin/python scripts/outcome/run_reviews.py --first 5
```

```bash
OUTCOME_PROFILE=cloud-hunk .venv/bin/python scripts/outcome/run_judgments.py --first 5
```

The judge script writes the tables for the PRs it was given. Output goes to `data/aacr_hunk/outcomes/qwen3.8-27b_aacr-hunk_v2/`. The first test's output is in `qwen3.8-27b_aacr-hunk_v1/` next to it.

## 10. Not done, and open

1. **Only five PRs have been reviewed** (Section 12). They hold 38 reference comments, 23 of them in one PR, so the numbers are a first reading and not a result.
2. **The levels cannot be told apart on five PRs** (Section 14.3). Two runs of the same input differ by about three covered references, as much as the levels differ. A larger set, or repeated runs, is needed before any level effect can be claimed.
3. **No priority or confidence** in the paper's reply format. A variant that adds them would allow filtering afterwards, at the cost of no longer being the paper's prompt.
4. **Two causes are still changed together** (the prompt and one hunk per call). Running this prompt once per PR would separate them. The parser can already find a quoted snippet's file in a whole-PR diff, but the per-PR context file would have to be rebuilt so that its levels record their parts, and that variant has not been run or tested end to end.
5. **How often the reviewer's quotes are found in the hunk:** 92 to 98% at the first three levels, 7 to 14% from `H04_pr_diff` on (Section 12.3). Findings whose quote is not found have no line numbers.
6. **The fixed code fence** (Section 5) breaks for hunks that contain a fence line.
7. **Large PRs:** the other-changes section is cut at 16,000 tokens for 10 of the 185 PRs.
8. **The selector for per-hunk cells is not designed.** Most hunks have no reference comment, so for them the only measurable outcome is a false alarm.
9. **Which reference set to score against** (v1 or v2) is open, as for the per-PR table.
10. **The 30-PR build** planned as the next step has not been started.

## 11. Where things are

| Path | Content |
|---|---|
| `src/tracer_context/hunks.py` | Splitting a diff into hunks, and repeating a trace from one hunk |
| `src/tracer_context/cbm.py` | `trace(..., per_seed_edges=True)` keeps the links |
| `src/tracer_context/build.py` | The other-changes section, the two optional config keys, and where each part of a level's text is (`input_sections`, `split_input`) |
| `src/tracer_context/dataset.py` | `pack_hunks`, `hunk_units`, the per-hunk record format |
| `configs/levels_hunk/` | The eleven per-hunk level configs |
| `configs/review_prompts/aacr_nonagent_no_context.md`, `aacr_nonagent_with_context.md` | The paper's two prompts |
| `src/review_outcomes/prompts.py` | `ReviewPrompt`, `ReviewInput`, `load_review_prompt` |
| `src/review_outcomes/parse.py` | `parse_tagged_review`, `locate_snippet` |
| `src/review_outcomes/table.py` | Per-hunk cells, `build_hunk_tables` |
| `notebooks/outcome_table.ipynb` | The `cloud-hunk` profile |
| `tests/test_hunk_context.py`, `tests/test_review_outcomes.py` | The new tests |
| `data/aacr_hunk/` (git-ignored) | This test's data: the five-PR per-hunk build with the current levels (`context_levels.jsonl`), the first test's build (`first_test/context_levels.jsonl`), and the outcome runs (`outcomes/qwen3.8-27b_aacr-hunk_v1/` is the first test) |
| `data/aacr/` (git-ignored) | The original per-PR levels and the first run, unchanged |

## 12. First test on five PRs (November 15, 2026)

**What was run.** The five PRs built in Section 7 (`cline__cline__5177`, `CherryHQ__cherry-studio__5637`, `browser-use__browser-use__857`, `timescale__timescaledb__7915`, `gofr-dev__gofr__1617`) were reviewed and judged on the cloud server with the `cloud-hunk` profile. They have 78 hunks and 38 reference comments. The results are in `data/aacr_hunk/outcomes/qwen3.8-27b_aacr-hunk_v1/` on the laptop and on the server.

### 12.1 The run itself

| Measure | Value |
|---|---|
| Cells (hunk and level) | 858, all usable |
| Model calls for the review | 601 (257 cells reused a call with the same input) |
| Input tokens | 5,245,646 |
| Review time | 1 h 11 min at 8 calls at once |
| Replies that needed a retry step | 9 of 601 (8 a longer output, 1 a follow-up); all 9 then gave an answer |
| Replies cut off at the output limit | 0 |
| Judge | 601 calls, 12 min 33 s, 0 judge errors |

Every reply was in the paper's tag format and could be read.

### 12.2 Recall and precision

**How the two runs differ, and what stays the same.** "Per hunk" and "per PR" name how the reviewer is called, not how recall is counted. Recall is counted the same way in both runs, so the columns below can be compared.

| | First run ("per PR") | This test ("per hunk") |
|---|---|---|
| Reviewer calls for one PR at one level | One, with the whole diff | One for each hunk |
| Prompt | Codex rubric | The paper's prompt |
| Recall | Covered references ÷ the PR's references | The same |

For one PR at one level, the findings of all its hunk reviews are put together. A reference comment counts as covered when a finding from any hunk matches it. The judge compares a hunk's findings with the PR's reference comments on the same file, so a finding from one hunk can cover a reference that sits on another hunk of that file. Every reference of the PR stays in the count, including those on files where no finding was written.

Recall is the share of the 38 reference comments that were covered. Precision is the share of the reviewer's findings that matched a reference comment. Both are summed over the five PRs. The per-PR columns are the first run's numbers on the same five PRs.

| Per-hunk level | Recall | Precision | Findings | Per-PR level | Recall | Precision | Findings |
|---|---|---|---|---|---|---|---|
| `H01_min` | 18 of 38 (47.4%) | 13.6% | 110 | `L01_min` | 2 of 38 (5.3%) | 12.5% | 8 |
| `H02_function` | 20 of 38 (52.6%) | 13.6% | 125 | `L02_function` | 4 of 38 (10.5%) | 50.0% | 6 |
| `H03_file` | 24 of 38 (63.2%) | 17.5% | 120 | `L03_file` | 5 of 38 (13.2%) | 50.0% | 8 |
| `H04_pr_diff` | 16 of 38 (42.1%) | 3.9% | 383 | (no such level) | | | |
| `H05_callees1` | 17 of 38 (44.7%) | 4.0% | 403 | `L04_callees1` | 3 of 38 (7.9%) | 23.1% | 13 |
| `H06_callers1` | 14 of 38 (36.8%) | 3.4% | 385 | `L05_callers1` | 2 of 38 (5.3%) | 28.6% | 7 |
| `H07_tests1` | 15 of 38 (39.5%) | 3.7% | 383 | `L06_tests1` | 2 of 38 (5.3%) | 25.0% | 8 |
| `H08_agent` | 17 of 38 (44.7%) | 4.1% | 386 | `L07_agent` | 6 of 38 (15.8%) | 71.4% | 7 |
| `H09_depth2` | 16 of 38 (42.1%) | 4.0% | 374 | `L08_depth2` | 4 of 38 (10.5%) | 50.0% | 6 |
| `H10_depth2_docs` | 15 of 38 (39.5%) | 3.6% | 387 | `L09_depth2_docs` | 5 of 38 (13.2%) | 57.1% | 7 |
| `H11_all` | 15 of 38 (39.5%) | 3.8% | 373 | `L10_all` | 3 of 38 (7.9%) | 75.0% | 4 |

- **The low recall of the first table came from the method, not from the model.** With the paper's prompt and one hunk per call, the same reviewer covers 47 to 63% of the references at the first three levels. The paper's range for plain LLMs is 24.25 to 47.24% recall at 5.10 to 11.30% precision.
- **Recall rises over the first three levels** (18, 20, 24 of 38). That is six references on five PRs, too few to call a level effect.
- **32 of the 38 references are covered at some level, and 9 at every level.** So which level is used changes the outcome for 23 references, which is what a selector needs.
- **The reviewer now always comments.** Two of the 858 cells have no finding. At the first three levels a hunk gets about 1.5 findings.

### 12.3 The problem from `H04_pr_diff` on

From `H04_pr_diff` on, each hunk gets about 5 findings instead of 1.5, precision falls to about 4%, and recall falls too.

The cause is visible in the quotes. The reviewer must quote the code each finding is about. At the first three levels 92 to 98% of the quotes are found in the hunk under review. From `H04_pr_diff` on, 7 to 14% are. The rest quote the PR's other hunks, which that level shows as context: the reviewer reviews the whole PR in every call, and the same finding comes back once per hunk.

| Level | Findings | Quote found in the hunk under review |
|---|---|---|
| `H01_min` | 110 | 108 (98%) |
| `H02_function` | 125 | 115 (92%) |
| `H03_file` | 120 | 114 (95%) |
| `H04_pr_diff` | 383 | 27 (7%) |
| `H05_callees1` to `H11_all` | 373 to 403 | 25 to 54 (7 to 14%) |

Dropping the findings whose quote is not in the hunk does not repair these levels. Counting only the findings that are in the hunk, `H04_pr_diff` covers 12 of 38 references and `H11_all` 9 of 38, below the 17 to 22 of the first three levels counted the same way. The other hunks take the reviewer's attention away from the hunk it was asked about.

Every level after `H04_pr_diff` contains the other hunks, so this test says nothing yet about callees, callers, tests or documents as context for a hunk.

### 12.4 By PR

References covered at each level, in the order `H01_min` to `H11_all`, and `L01_min` to `L10_all` for the first run.

| PR | Hunks | References | Per hunk, `H01` to `H11` | Per PR, `L01` to `L10` |
|---|---|---|---|---|
| `CherryHQ__cherry-studio__5637` | 11 | 23 | 9, 11, 13, 8, 9, 7, 7, 8, 7, 7, 7 | 0 at every level |
| `timescale__timescaledb__7915` | 19 | 6 | 3, 2, 4, 1, 2, 1, 1, 2, 2, 2, 1 | 0 at every level |
| `gofr-dev__gofr__1617` | 30 | 5 | 4, 4, 4, 5, 4, 4, 5, 5, 5, 4, 4 | 2, 3, 3, 1, 0, 0, 4, 3, 3, 1 |
| `browser-use__browser-use__857` | 17 | 2 | 1, 2, 2, 1, 1, 1, 1, 1, 1, 1, 1 | 0, 1, 1, 1, 1, 1, 1, 1, 1, 1 |
| `cline__cline__5177` | 1 | 2 | 1, 1, 1, 1, 1, 1, 1, 1, 1, 1, 2 | 0, 0, 1, 1, 1, 1, 1, 0, 1, 1 |

- The gain is largest where the first run found nothing: two PRs with 29 references between them went from 0 covered to 9 to 17 covered at the first three levels.
- `gofr-dev__gofr__1617` is the one PR where the first run was already close at its best levels (4 of 5 at `L07_agent`).
- Findings written per PR at `H01_min`: 21, 23, 43, 21 and 2, in the order of the table. From `H04_pr_diff` on: 61 to 77, 65 to 71, 150 to 165, 83 to 102, and 2 to 3. `cline__cline__5177` has one hunk, so it has no other hunks to show and its findings do not grow.

### 12.5 By kind of reference

References covered, of those of each kind.

| Kind | References | `H01_min` | `H02_function` | `H03_file` | `H04_pr_diff` | `H11_all` |
|---|---|---|---|---|---|---|
| Context label: Diff Level | 21 | 9 | 11 | 12 | 7 | 7 |
| Context label: File Level | 13 | 6 | 7 | 8 | 7 | 6 |
| Context label: Repo Level | 4 | 3 | 2 | 4 | 2 | 2 |
| Category: Code Defect | 23 | 11 | 12 | 15 | 10 | 9 |
| Category: Maintainability and Readability | 11 | 4 | 5 | 6 | 4 | 4 |
| Category: Performance | 4 | 3 | 3 | 3 | 2 | 2 |
| Written by an LLM | 31 | 16 | 16 | 21 | 12 | 11 |
| Written by a person | 7 | 2 | 4 | 3 | 4 | 4 |

- Every kind is covered more often than in the first run, which covered 2 to 6 of all 38.
- The groups are too small to compare kinds with each other. Four Repo Level references and seven written by a person cannot show whether wider context helps them.

### 12.6 The stricter count per hunk

`hunk_table.csv` has one row per hunk and level. It counts a reference only for the hunk whose lines it sits on, and only when that hunk's own findings cover it.

- 17 of the 78 hunks have a reference on their lines. The other 61 have none, so any finding on them counts against precision.
- References covered by the review of their own hunk, of 38: 16 at `H01_min`, 17 at `H02_function`, 22 at `H03_file`, 13 at `H04_pr_diff`, 13 at `H05_callees1`, 8 at `H06_callers1`, 8 at `H07_tests1`, 13 at `H08_agent`, 12 at `H09_depth2`, 12 at `H10_depth2_docs`, 10 at `H11_all`.
- At the first three levels this count is 2 below the PR count of Section 12.2 (16, 17, 22 against 18, 20, 24), so almost all of the recall there comes from hunks reviewing their own code.

### 12.7 Limits of this reading

- Five PRs and 38 references. `CherryHQ__cherry-studio__5637` holds 23 of them.
- The judge is the same model as the reviewer and has not been validated, as in the first run.
- More findings give the judge more chances to match. Precision is the check on that: 14 to 18% at the first three levels.
- References are the v1 set.
- The first run and this test differ in two things at once, the prompt and one hunk per call (Section 10, point 4). This test does not say how much of the gain each one gives.

## 13. Changes after the first test (November 15, 2026)

**Status: built, tested and run. The results are in Section 14.**

The first test showed that a reviewer given the PR's other hunks as a diff reviews those instead of its own hunk (Section 12.3). Four things were changed for that.

| Change | Before | Now |
|---|---|---|
| The other hunks | Level 4 (`H04_pr_diff`), as a diff, inside every later level | Out of the chain. A list without code is the last level, `H11_pr_summary` |
| The chain | Callees, callers, tests and the rest were built on top of the other hunks | `H04_callees1` to `H10_all` are built on `H03_file`, and match `L04_callees1` to `L10_all` |
| What else the PR changes | Only visible as the other hunks' diff | From `H02_function` on, a context block's header names the lines the PR changes in it outside the hunk |
| Which findings count | All of them | Only those whose quoted code is found in the hunk under review |

**Why the list and the marker.** Taking the other hunks out could hide a change this hunk depends on. Most of that is kept without them: the context is the code after the change, so a related change in the same function, the same file, a callee, a caller or a test is shown by the level that shows that code. Two things would still be lost. The marker gives back which lines are new. The list level tests whether changes with no code link to the hunk matter.

**Why the counting rule.** At the first three levels of the first test the rule would have dropped 2 to 8% of the findings and 1 or 2 of the covered references. It removes repeated findings about other code at every level, whatever the context shows.

### 13.1 What was checked

- **Tests:** 210 pass. They cover the list form, the marker, the diff form that is still available, the new config keys and the counting rule.
- **The five PRs were rebuilt** in 1 min 36 s, with no failure and no nesting or size problem in any of the 858 cells. The first test's context file is kept as `data/aacr_hunk/first_test/context_levels.jsonl` (sha256 `9835052d…`). The new file's sha256 is `a4b425c6…`.
- **`H01_min` is unchanged:** all 78 inputs are identical to the first test's.
- **The marker appears where expected:** in 21 of the 78 hunks at `H02_function`, 48 at `H03_file` and 63 at `H10_all`. The other inputs at those levels are identical to the first test's (57 of 78 at `H02_function`, 30 at `H03_file`).
- **The list appears in 77 of the 78 hunks** at `H11_pr_summary`. The one without it is the only hunk of its PR.

### 13.2 Size of the inputs

Input tokens summed over the 78 hunks.

| Level | Now | First test, same position |
|---|---|---|
| `H01_min` | 41,391 | 41,391 |
| `H02_function` | 79,360 | 78,948 |
| `H03_file` | 153,420 | 151,566 |
| `H04_callees1` | 221,117 | 565,561 (`H04_pr_diff`) |
| `H05_callers1` | 286,933 | 632,944 |
| `H06_tests1` | 322,150 | 698,383 |
| `H07_agent` | 701,613 | 733,325 |
| `H08_depth2` | 785,812 | 1,111,979 |
| `H09_depth2_docs` | 833,529 | 1,196,025 |
| `H10_all` | 966,995 | 1,243,612 |
| `H11_pr_summary` | 1,008,382 | 1,376,965 |
| All levels | 5,400,702 | 7,830,699 |

The first test's levels from position 5 on are one step behind the new ones (its `H05_callees1` is the new `H04_callees1` plus the other hunks), so the rows of the lower half compare positions, not the same context.

### 13.3 The next run

A dry run of the `cloud-hunk` profile on the rebuilt file plans 601 reviewer calls with 4,118,505 input tokens, against 5,245,646 in the first test. Every cell is sent again, including `H01_min`, because the counting rule is part of each review's saved key. The output goes to a new folder, `qwen3.8-27b_aacr-hunk_v2/`, and the first test's folder is left as it is.

The server needs the branch pulled and the new context file copied to `data/aacr_hunk/context_levels.jsonl`.

What to look at in the results:

1. Whether `H04_callees1` to `H10_all` now keep or raise the recall of `H03_file` (24 of 38 in the first test), instead of falling.
2. How many findings are set aside as not on the hunk, per level.
3. Whether `H11_pr_summary` covers references that `H10_all` misses, and whether the list brings the off-hunk findings back.

## 14. Second test on the five PRs (November 15, 2026)

**What was run.** The same five PRs, 78 hunks and 38 reference comments as the first test, with the levels and the counting rule of Section 13. Reviewer, judge, prompt and settings are unchanged. The results are in `data/aacr_hunk/outcomes/qwen3.8-27b_aacr-hunk_v2/` on the laptop and on the server.

### 14.1 The run itself

| Measure | Second test | First test |
|---|---|---|
| Cells, all usable | 858 | 858 |
| Model calls for the review | 601 | 601 |
| Input tokens | 4,118,505 | 5,245,646 |
| Review time | 36 min | 1 h 11 min |
| Replies that needed a retry step | 0 | 9 |
| Judge | 601 calls, 6 min 16 s, 0 errors | 601 calls, 12 min 33 s, 0 errors |

### 14.2 Recall and precision

Counted as in Section 12.2. "Set aside" is the number of findings that did not quote the hunk under review and were therefore not counted.

| Level | Recall | Precision | Findings counted | Set aside | First test at the same context |
|---|---|---|---|---|---|
| `H01_min` | 21 of 38 (55.3%) | 16.7% | 114 | 2 | 18 of 38, 13.6% |
| `H02_function` | 19 of 38 (50.0%) | 14.8% | 115 | 15 | 20 of 38, 13.6% |
| `H03_file` | 22 of 38 (57.9%) | 17.4% | 115 | 18 | 24 of 38, 17.5% |
| `H04_callees1` | 19 of 38 (50.0%) | 14.9% | 114 | 16 | 17 of 38, 4.0% (with the other hunks) |
| `H05_callers1` | 20 of 38 (52.6%) | 15.5% | 116 | 20 | 14 of 38, 3.4% |
| `H06_tests1` | 21 of 38 (55.3%) | 16.2% | 117 | 21 | 15 of 38, 3.7% |
| `H07_agent` | 17 of 38 (44.7%) | 13.9% | 108 | 11 | 17 of 38, 4.1% |
| `H08_depth2` | 17 of 38 (44.7%) | 14.2% | 106 | 16 | 16 of 38, 4.0% |
| `H09_depth2_docs` | 16 of 38 (42.1%) | 13.2% | 106 | 16 | 15 of 38, 3.6% |
| `H10_all` | 16 of 38 (42.1%) | 13.0% | 108 | 23 | 15 of 38, 3.8% |
| `H11_pr_summary` | 19 of 38 (50.0%) | 15.1% | 106 | 34 | (no such level) |

- **The fall is gone.** Every level writes 106 to 117 counted findings, where the first test wrote 373 to 403 from its level 4 on. Precision stays at 13 to 17% at every level instead of dropping to about 4%.
- **The per-PR run on these PRs covered 2 to 6 of 38** (Section 12.2). Every level here covers 16 to 22.
- **The summary level did not bring the problem back.** It has the most findings set aside (34), but its counted findings and precision are like the others'. It covers 5 references that `H10_all` misses and misses 2 that `H10_all` covers, which is within the noise of Section 14.3.
- **28 of the 38 references are covered at some level, and 10 at every level.**
- **Almost all recall comes from a hunk reviewing its own lines:** the stricter count of Section 12.6 is within 2 of the PR count at every level (for example 22 of 22 at `H03_file`).

By PR, references covered at `H01_min` to `H11_pr_summary`:

| PR | References | Covered |
|---|---|---|
| `CherryHQ__cherry-studio__5637` | 23 | 11, 10, 10, 7, 8, 8, 6, 6, 6, 6, 8 |
| `timescale__timescaledb__7915` | 6 | 4, 2, 5, 5, 5, 5, 4, 4, 4, 4, 4 |
| `gofr-dev__gofr__1617` | 5 | 4, 4, 4, 4, 4, 5, 4, 4, 4, 4, 4 |
| `browser-use__browser-use__857` | 2 | 1, 2, 2, 2, 2, 2, 2, 1, 1, 1, 2 |
| `cline__cline__5177` | 2 | 1, 1, 1, 1, 1, 1, 1, 2, 1, 1, 1 |

By context label, at `H01_min`, `H03_file`, `H06_tests1` and `H10_all`: Diff Level 12, 11, 8, 8 of 21; File Level 5, 7, 9, 5 of 13; Repo Level 4, 4, 4, 3 of 4.

### 14.3 The levels cannot be told apart yet

`H01_min` has the same 78 inputs in both tests, so the two tests are two runs of the same thing at that level.

- **32 of the 78 replies are word for word the same.** The other 46 differ, although the temperature is 0. The model server does not give repeatable replies when calls run in parallel (noted for the first run).
- **Covered references: 18 in the first test, 21 in the second, 17 in common.** The counting rule set aside 2 findings, so it does not explain the difference.

So one run moves by about three covered references with nothing changed. The levels differ by 16 to 22, a spread of six. No difference between two levels in the table above is larger than what two runs of one level can show.

Two patterns are worth checking on more data, and neither is a finding yet:

- **Recall is lower from `H07_agent` on** (16 or 17, against 19 to 22 before it). It comes from one PR, `CherryHQ__cherry-studio__5637`, which goes from 10 or 11 covered to 6. The inputs grow most at that level (from 322,150 to 701,613 tokens over the 78 hunks).
- **`H03_file` is the best level in both tests** (24 and 22).

### 14.4 What this test settles, and what it does not

- **Settled:** one hunk per call with the paper's prompt gives recall of 42 to 58% on these PRs at every level, against 5 to 16% per PR. Showing the other hunks as a diff was the cause of the first test's fall, and the changes of Section 13 remove it.
- **Not settled:** whether any context level is better than another for a hunk. Five PRs and one run per cell cannot show it. That needs more PRs, and a measure of the run-to-run noise (the same cells run two or three times) to know how large a difference must be.
- **Unchanged limits:** the judge is the reviewer's own model and is not validated; the references are the v1 set; the prompt and the hunk-by-hunk calling were changed together.

## 15. Thinking on, two levels (November 15, 2026)

**What was run.** The second test's five PRs at `H01_min` and `H03_file` only, with the reviewer's thinking on (profile `cloud-hunk-think`, run with `--levels H01_min H03_file`). The output limit was raised from 4,096 to 16,000 tokens so that thinking does not cut replies off. Inputs, prompt, counting rule and judge (thinking off) are those of the second test. The results are in `data/aacr_hunk/outcomes/qwen3.8-27b-think_aacr-hunk_v2/`.

Two levels were run, not eleven, to keep the test short: 156 cells and 154 calls.

### 15.1 Results

"Off" columns are the two thinking-off runs of the same level: the second test, then the first test.

| Measure | `H01_min`, thinking on | `H01_min`, off | `H03_file`, thinking on | `H03_file`, off |
|---|---|---|---|---|
| References covered, of 38 | 24 (63.2%) | 21, 18 | 27 (71.1%) | 22, 24 |
| Precision | 10.4% | 16.7%, 13.6% | 11.8% | 17.4%, 17.5% |
| Findings counted | 201 | 114, 110 | 195 | 115, 120 |
| Findings set aside (not on the hunk) | 6 | 2 | 98 | 18 |
| Hunks with no finding | 8 | 0, 0 | 8 | 2, 1 |
| Most findings on one hunk | 12 | 5, 5 | 10 | 6, 6 |
| Output tokens | 273,633 | 30,884 | 444,738 | 37,657 |

- **Recall is higher at both levels,** by 3 to 6 references over the thinking-off runs. That is at the edge of the run-to-run difference of about three (Section 14.3), but it goes the same way at both levels and exceeds both earlier runs each time.
- **The reviewer writes almost twice as many findings,** and precision falls from 14 to 17% to 10 to 12%. More findings give more chances to match, so part of the recall gain may come from volume alone.
- **Findings spread out.** Eight hunks get no finding, where thinking-off runs almost always wrote one, and some hunks get up to 12.
- **With file context, 98 findings were set aside** as not about the hunk under review, against 18 with thinking off. The thinking reviewer comments much more on the surrounding code. The counting rule keeps these out of the score.
- **References covered with thinking on that neither thinking-off run covered:** 7 at `H01_min` and 4 at `H03_file`. The other way round: 5 and 2.
- **By context label at `H03_file`:** Diff Level 13 of 21, File Level 10 of 13, Repo Level 4 of 4.

### 15.2 Cost

Measured from the saved call times of the two levels, 78 calls each, eight calls in parallel.

| Level | Thinking off | Thinking on | Slower by |
|---|---|---|---|
| `H01_min` | 20 s per call, 3.2 min in all | 146 s per call, 23.8 min in all | 7 times |
| `H03_file` | 25 s per call, 4.1 min in all | 238 s per call, 38.6 min in all | 9 times |
| Both | 7.3 min | 62.4 min | 8.5 times |

- **The run's own log says 1 h 08 min** for the thinking-on review, which includes two retried replies and the start.
- **The cause is output, not input.** The server writes about 175 tokens per second over the eight parallel calls with or without thinking. With thinking on a call writes about 4,600 tokens (its reasoning, then the review) against about 440.
- **Two replies were cut off** at the 16,000-token limit and completed by the retry step. None was cut off with thinking off.
- **Memory use is unchanged.**

Scaled up, assuming the same slowdown holds at the wider levels (measured at two levels only):

| Run | Thinking off | Thinking on (estimate) |
|---|---|---|
| Five PRs, all eleven levels | 36 min (measured) | about 5 hours |
| Whole benchmark, all levels | about a day (estimate) | a week or more |

### 15.3 Reading

Thinking raises recall on this sample and lowers precision, and it costs about 8.5 times the review time. Whether the recall gain is more than extra volume cannot be said from one run of two levels on five PRs. It is not adopted. If it is tested further, the comparison that matters is recall at equal numbers of findings, or the severity-weighted score proposed on the branch `review-score`.

## 16. Why the levels do not differ, and what the measures miss (November 15, 2026)

Checks made on the second test's saved results, with no new model calls.

### 16.1 Why recall does not rise with the level

1. **Run-to-run noise is as large as the level differences.** Two runs of identical `H01_min` inputs disagree on 5 of the 38 references. Neighbouring levels disagree on 1 to 7.
2. **Most references need no context to be found.** By AACR's context label, `H01_min` covers 12 of the 21 Diff Level references, 5 of the 13 File Level ones and all 4 Repo Level ones. Of the 11 references covered at no level, 7 are Diff Level.
3. **The reviewer writes a fixed number of findings, so context replaces findings.** It writes 106 to 117 counted findings at every level, about 1.5 per hunk. The share of findings that name code which only the context shows is 0% at `H01_min`, 17% at `H02_function`, 41% at `H03_file` and 61% at `H10_all`. (A word match: names written in code style in the finding, present in the level's context and absent from the hunk and the PR text. It shows the context is used, not that the remark is right.) A finding about the hunk is exchanged for one about the wider code, and the references mostly describe problems visible in the hunk.
4. **References are dense where they exist.** The 38 references sit on 17 of the 78 hunks; two hunks hold 7 each. Those 17 hunks get 32 to 41 findings in total, so recall is capped: at most 28 to 34 references could be covered even if every finding matched a different one.

Context does help in single cases. Two references, both written by people and labelled File Level, are missed at `H01_min` and covered from `H02_function` on in both tests.

By pattern over the ten nested levels: 11 references are covered at every level, 11 at none, 3 are gained at some level and kept, 5 are lost at some level and stay lost, and 8 go back and forth. Neighbouring levels often have identical inputs and share one reply, so a pattern that holds over several levels is weaker evidence than it looks.

### 16.2 What recall and precision do not measure

- **Recall** is agreement with a list, decided by an unvalidated judge. Of six matches read at `H03_file`, five were clearly the same problem and one was loose.
- **Precision** counts every unmatched finding as wrong. Of nine unmatched findings read at `H01_min`, four were plausible real points (two of them substantive), three were speculative requests to check something that the hunk cannot answer, and two complained that the rest of the PR was not shown. This is one reader's judgement of a small sample.
- **61 of the 78 hunks hold no reference,** so every finding on them (about 75 per level) counts against precision by construction.
- **A rough word count** suggests context reduces one kind of noise: findings that say the shown code is incomplete fall from 17% at `H01_min` to 12% at `H10_all` and 4% at `H11_pr_summary`.

So 13 to 17% precision is a lower bound on how many findings are right, not a measurement of it.

### 16.3 What follows

- **Repeat cells** and use the rate over runs, not one run, wherever levels are compared or a selector is trained.
- **More PRs,** so that groups such as File Level references are large enough to compare.
- **A measure that can show what context does:** rate each finding as valid, speculative or wrong, and weight references by severity. The design is on the branch `review-score` (`docs/review_score_design.md` there).
- **The five PRs were picked by hand** when the code was first checked, for a spread of languages. They are not a random sample, and one of them holds 23 of the 38 references. The 30-PR set is to be drawn at random with a fixed seed, with these five added and marked.

