# Per-hunk review test (branch `per-hunk-review`, started November 15, 2026)

**Status: the code is written and tested on the laptop, and five PRs have been built per hunk. No review has been run, because the cloud server is off. Nothing here changes the per-PR pipeline, its data or its table. The work is on the branch `per-hunk-review` and is not merged.**

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
| Not yet | A review run, a judge run, and the comparison with the per-PR table. |

## 3. What one review covers

- **A hunk** is one `@@` block of the diff the reviewer sees. That diff has three unchanged lines around each change, as GitHub shows it. The 185 built PRs have 4,136 hunks: 22.4 per PR on average, median 10.
- **A cell** is one (hunk, level) pair. A hunk's id is its PR's id and its position in the diff, for example `gofr-dev__gofr__1617__h0004`.
- **Files without a hunk** have no cell: binary files, pure renames and mode changes.

The data set's `stats.hunks` field counts something else (runs of changed lines with no surrounding lines) and must not be used for this.

## 4. The levels, per hunk

The eleven levels are in `configs/levels_hunk/`. They mirror the ten per-PR levels and add one, `H04_pr_diff`.

| Level | What it adds |
|---|---|
| `H01_min` | The PR's title and description, and this hunk. This is the paper's setting without code context. |
| `H02_function` | The full function or class this hunk changes |
| `H03_file` | The file this hunk is in (windows around the hunk for files over 400 lines) |
| `H04_pr_diff` | The PR's other hunks, as a diff after this hunk |
| `H05_callees1` | Definitions of what this hunk's code calls directly |
| `H06_callers1` | Direct callers and users of this hunk's code |
| `H07_tests1` | Tests that directly exercise this hunk's code |
| `H08_agent` | Repository guide files, and text-search hits for the names this hunk changes |
| `H09_depth2` | Callees and callers two hops from this hunk |
| `H10_depth2_docs` | Documentation sections and tests up to two hops away |
| `H11_all` | Everything the tracer finds from this hunk, up to three hops, with no item limits |

Item limits, the 256,000-token input cap, the tokenizer and the rendering are those of the per-PR levels.

### 4.1 Which changed symbols belong to a hunk

A hunk's changed symbols are those that overlap its changed lines.

The tracing tool has a second rule, which the per-hunk levels keep. When no change in a file touches a symbol (only an import line changed, for example), the tool takes every symbol of that file as changed and marks it `whole_file`. A hunk of such a file gets all of them.

This rule also shapes the per-PR levels and was not written down before: `L02_function` shows every function of a file whose only change is outside all of them. It was found here because 43 of one PR's 54 changed symbols had this mark.

### 4.2 How the trace is repeated from one hunk

The tool walks from all of a PR's changed symbols at once and reports each caller's nearest changed symbol only. For this test the trace also keeps every link among the traced symbols (`call_edges`, `caller_edges`). The walk is then repeated from one hunk's symbols over those links, so each hunk gets its own callers, callees and hop counts. A PR is still traced once.

- **Another changed function can be a hunk's caller or callee.** In the per-PR levels it is only ever shown as changed code.
- **Text-search hits** are kept for the names of the hunk's own symbols.
- **Check built in:** every build compares a walk from all changed symbols over the kept links with the tool's own walk, and warns on any difference.

### 4.3 The other-changes level

A per-PR review sees the whole diff. A per-hunk review does not, so the rest of the diff becomes context. `H04_pr_diff` shows it in a section named "Other changes in this pull request", between the hunk and the repository context.

- **Order:** nearest first, meaning the other hunks of the same file by distance, then the other files in diff order.
- **Room:** 16,000 tokens. That holds the whole diff of 175 of the 185 built PRs. When hunks are left out, the section says how many are shown.
- **Stable:** every later level shows the same section, so the levels stay nested.

### 4.4 What the per-PR pipeline keeps

- **A data folder of its own.** Everything of this test lives in `data/aacr_hunk/`: its copy of the AACR file, its context levels, its logs and its outcome runs. `data/aacr/`, with the per-PR levels and the first run, is not written to.
- **A guard.** A build refuses a folder whose context file holds the other unit's records. So `--unit hunk` pointed at `data/aacr` stops before writing anything, and the original `data/aacr/context_levels.jsonl` cannot be replaced by mistake. On November 15 that file still had the hash the first run recorded (`b06dee5e…`).
- **Configs:** the two new keys (`input.other_hunks`, `packing.other_hunks_max_tokens`) are optional. A config without them is unchanged, and so is its hash.
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

- **Judge:** unchanged. Each of a hunk's findings (the text of its `<note>`) is compared with the PR's reference comments on the same file.
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

The judge script writes the tables for the PRs it was given. Output goes to `data/aacr_hunk/outcomes/qwen3.8-27b_aacr-hunk_v1/`.

## 10. Not done, and open

1. **No review has been run.** Every claim above is about inputs and code, not about review quality.
2. **The pilot is not designed:** which PRs, which levels, and what result would justify the full run.
3. **No priority or confidence** in the paper's reply format. A variant that adds them would allow filtering afterwards, at the cost of no longer being the paper's prompt.
4. **Two causes are still changed together** (the prompt and one hunk per call). Running this prompt once per PR would separate them. The parser can already find a quoted snippet's file in a whole-PR diff, but the per-PR context file would have to be rebuilt so that its levels record their parts, and that variant has not been run or tested end to end.
5. **How often the reviewer's quotes are found in the hunk** is unknown until replies exist. Findings whose quote is not found have no line numbers.
6. **The fixed code fence** (Section 5) breaks for hunks that contain a fence line.
7. **Large PRs:** the other-changes section is cut at 16,000 tokens for 10 of the 185 PRs.
8. **The selector for per-hunk cells is not designed.** Most hunks have no reference comment, so for them the only measurable outcome is a false alarm.
9. **Which reference set to score against** (v1 or v2) is open, as for the per-PR table.
10. **The branch is local.** It is not pushed, and the server has neither the branch nor the per-hunk context file.

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
| `data/aacr_hunk/` (git-ignored) | This test's data: the five-PR per-hunk build, and later its outcome runs |
| `data/aacr/` (git-ignored) | The original per-PR levels and the first run, unchanged |
