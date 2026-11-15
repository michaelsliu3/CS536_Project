# Review score design (branch `review-score`, started November 15, 2026)

**Status: the equation and its starting values were agreed on November 15, 2026, and the code for the pilot is written and tested (Section 11). The 38 reference comments of the five test PRs are labelled and the three finished per-hunk runs are re-scored per tier (Section 12). The rating of findings was tried and dropped the same day (Section 13), which reduced the score to its first term. The branch `review-score` was created from `per-hunk-review` to hold it.**

This document records why a new score is wanted, which signals it uses, how it is computed, which numbers in it are choices, and how those choices are meant to be made and checked.

## 1. Why a new score

The outcome tables report recall and precision as AACR-Bench defines them. Both have limits that the per-hunk tests made visible ([per_hunk_review_test.md](per_hunk_review_test.md), Sections 12 and 14).

- **Every reference comment counts the same.** Of the benchmark's 1,506 reference comments, 626 (42%) are labelled Maintainability and Readability. A remark about naming counts as much as a race condition.
- **Recall measures agreement with a list.** A reference is covered when the judge says a finding matches it. The judge is the reviewer's own model and is not validated.
- **Precision counts every unmatched finding as wrong.** In the second per-hunk test, 61 of the 78 hunks hold no reference comment, so all 76 findings on them count against precision by construction. A reading of nine unmatched findings at `H01_min` found four plausible real points, three speculative requests to check something, and two complaints that the rest of the PR was not shown. That reading is one person's judgement of a small sample.
- **The levels cannot be told apart on recall.** Recall against these references mostly rewards remarks about the hunk itself, and the reviewer writes about the same number of findings at every level.

What a developer wants from a review is different: it should find the problems that block a merge, such as a build break, wrong behaviour or a broken API. Remarks on comment wording, spacing or a missing newline are worth little, and wrong or speculative remarks cost the reader time.

**Decided on November 15, 2026:** design a score that weights each comment by how much it matters, and label severity for that. Recall and precision stay in every table as the comparison with the paper.

## 2. Timeline

| Date | What happened |
|---|---|
| November 15 | Two per-hunk tests showed the limits above. The score was proposed and the branch `review-score` created. Decided that membership in AACR-Bench's v2 set is not used as a signal of a comment's value. |
| November 15, later | The equation of Section 4 and the starting values of Section 5 were agreed. The work itself is not to be started yet. |
| November 15, later | Decided that Qwen3.8-27B labels the references and rates the findings (Section 6.3). |
| November 15, later | Qwen3.8-27B labelled the 38 references with thinking off and on. Decided that the thinking-on labels are used, and that labelling always runs with thinking on (Section 12). The three finished runs were re-scored per tier. |
| November 15, later | The findings of two levels were rated with thinking off. Decided to drop the rating of findings and with it the second and third terms of the score (Sections 4 and 13). |
| November 15, later | Work started. `per-hunk-review` merged into this branch. The labelling, rating and scoring code written with tests (Section 11). |

## 3. Signals

The benchmark gives each reference comment a category, a context label, its author (a person, or an LLM and which one), a file and a line range. It has no severity.

| Signal | Available now | What it says | Weakness |
|---|---|---|---|
| **Severity**, labelled once with a written rubric (Section 6) | No. One model call per reference | Whether the problem blocks a merge | A model's judgement, so it must be checked against a person's labels |
| **Category** | Yes: Code Defect 710, Maintainability and Readability 626, Performance 117, Security Vulnerability 53 | The kind of problem | Coarse. Code Defect covers a crash and a minor edge case alike |
| **Written by a person** | Yes: 392 of 1,506 | A reviewer raised it in the real review | Says who wrote it, not how serious it is |
| **Acted on**: the commented code changed between the reviewed commit and the merge | No. Git work per PR | The author accepted the remark | Only meaningful for comments the author saw, so the 392 written by people. A nit is cheap to fix, so this shows a remark was accepted more than that it was serious |

**Not used: membership in AACR-Bench's v2 set.** The v2 set keeps 944 of the 1,506 comments and drops 430 of the 626 Maintainability comments. The repository does not say how the subset was chosen, so it is not taken as evidence that a comment is good or bad.

**What each signal is for.** Severity carries the weight. Category, authorship and "acted on" describe other things (the kind of problem, and how much real-world evidence stands behind the reference), so they enter as small multipliers that are neutral by default, and as checks on the severity labels (Section 7).

## 4. The score

**Decided on November 15, 2026:** the score is recall in which a reference comment counts by how much it matters. The reviewer's findings that match no reference are not scored (Section 13 has what was tried and why it was dropped).

```
importance(reference) = severity weight × category multiplier × evidence multiplier

score = Σ importance of the covered references ÷ Σ importance of all references
```

With the starting values of Section 5 the multipliers are 1, so the importance is the severity weight.

**An example.** A PR has three references: one blocking (5), one should-fix (2), one nit (0.5). The review covers the blocking one and the nit.

| Part | Value |
|---|---|
| Covered references | 5 + 0.5 = 5.5 |
| All references | 5 + 2 + 0.5 = 7.5 |
| Score | 5.5 ÷ 7.5 = 0.73 |

Plain recall for the same review is 2 of 3, or 0.67. Covering only the should-fix reference would score 0.27; covering only the blocking one, 0.67.

- **Over several PRs** the two sums are taken over all their references, as plain recall is.
- **Token cost** is not part of this score. The selector's reward subtracts it separately.
- **The score does not see how many findings a review makes.** A review that covers the same references with four times the findings scores the same. Precision, as AACR-Bench defines it, stays in every table for that.

**What is always reported next to the score,** so that no conclusion rests on the weights:

- references covered per severity tier, blocking first;
- the score under the five weightings of Section 5.1;
- plain recall and precision as before.

## 5. The numbers that are choices

Every signal has a number, and the numbers are few on purpose. Two are neutral at the start.

| Choice | Meaning | Starting value |
|---|---|---|
| Severity weights | Worth of a blocking, should-fix and nit comment | 5, 2, 0.5 |
| Category multiplier | Extra weight for a category, on top of severity | 1 for every category (neutral) |
| Evidence multiplier | Extra weight for a reference written by a person, and more if it was acted on | 1 (neutral); the first value to try is 1.25 and 1.5 |

A covered reference counts in full: the judge answers yes or no, and a graded match is not used. The first version of the score also had a credit for valid findings outside the references and a penalty for noise, both at 0.5; they left with the rating of findings (Section 13).

**The starting values were agreed on November 15, 2026.** The methods of Section 5.1 check them. A value changes only through those methods, with the date and the reason recorded here, and never to make a comparison come out a certain way.

### 5.1 How the numbers are set

**They are parameters of the measure, not of a model.** A model's hyperparameters are tuned to raise a result. These numbers define what the result means, so tuning them until the levels separate would fit the measure to the answer that is hoped for. They are set from what a developer values, written down with a date, and then left alone.

Four methods, used together:

1. **Exchange-rate questions.** A developer answers a few direct questions, and the answers fix the ratios.
   - How many nits are worth one blocking problem found? (With 5 and 0.5, ten.)
   - How many should-fix problems are worth one blocking problem? (With 5 and 2, two and a half.)
2. **A check against a person's preferences.** Take about 40 pairs of reviews of the same hunk, from different levels or runs. A developer picks the better review of each pair without seeing the score. The share of pairs where the score agrees is reported. A model fitted to half of the choices (a logistic regression on the difference between the two reviews' covered references per tier) shows which weights the choices imply, and the other half tests them.
3. **Several weightings instead of one.** Every comparison is reported under a small fixed set of weightings, and a conclusion counts as robust only if it holds under all of them.

   | Name | Blocking, should fix, nit |
   |---|---|
   | Flat | 1, 1, 1 (plain recall) |
   | Linear | 3, 2, 1 |
   | Starting values | 5, 2, 0.5 |
   | Steep | 25, 5, 1 |
   | Blocking only | 1, 0, 0 |

   The neutral multipliers are varied the same way (for example Security Vulnerability at 1.5, evidence at 1.25 and 1.5).
4. **A rule that needs no weights.** If one level covers at least as many references in every tier as another, it is better under any weights that respect the order of the tiers. Where this holds, the weights do not matter; where it does not, the answer depends on them and the write-up says so.

**Precedent.** Machine-translation evaluation scores errors by severity in the same way (the MQM scheme). The weights in use there differ between groups: minor 1 with major 5 or 10, and 25 for an unusable segment. That supports a steep scale, and it shows that even an established scheme has no single correct set of numbers, which is why method 3 is needed. For code review, a study at Microsoft treated a comment as useful when it led to a code change, which is the "acted on" signal. Both were read through later papers that cite them, and the originals must be checked before either is cited in a report:

- MQM weights as used in recent work: https://arxiv.org/html/2403.12666v1 and https://arxiv.org/pdf/2403.02366
- Bosu, Greiler and Bird, "Characteristics of Useful Code Reviews: An Empirical Study at Microsoft", MSR 2015: https://www.microsoft.com/en-us/research/?p=168510

## 6. Severity labels

### 6.1 The rubric

| Tier | A comment belongs here when the problem it names |
|---|---|
| **Blocking** | breaks the build or the tests, gives wrong behaviour on a normal path, breaks an API or its callers, opens a security hole, or can lose or corrupt data |
| **Should fix** | is a real defect with limited effect: a rare path, missing error handling, a real but bounded performance cost, a resource that is not released |
| **Nit** | concerns naming, comment wording, formatting, style, or a refactoring that changes no behaviour |

The label is for the problem if the comment is right. Whether the comment is right is a separate question, asked of the reviewer's findings (valid, speculative or wrong) and not of the references.

### 6.2 How a label is made

- **Input:** the comment, and the code it is about at the reviewed commit.
- **Not shown:** who wrote the comment, and which model.
- **Instruction:** judge from the code, not from the comment's wording. Many LLM-written references open with words like "critical" or "serious".
- **Output:** a tier, one sentence of reason, and a confidence.
- **Made once and saved** to a file with the rubric's version, the labeller's name and the date. Every score reads that file.

### 6.3 Who labels

**Decided on November 15, 2026: Qwen3.8-27B labels the reference comments, with thinking on** (Section 12.1), on the cloud server. No Claude model is used. The reviewer and the judge run with thinking off; the settings of all steps are listed in Section 18 of [per_hunk_review_test.md](per_hunk_review_test.md).

- **Known weakness:** 162 references were written by another Qwen model (Qwen-Coder-480B), and the labeller may rate those higher. One set of labels serves every level and run, so a bias moves all of them alike.
- **The option not taken** was a Claude model through the plan's included usage: stronger and independent of the reviewer, at no API bill, but that route has never been run. It stays available as a check on a sample (Section 7).

## 7. How the labels are checked

A label from a model changes the score, so it is treated like the judge: an input that must be validated.

- **Agreement with a person.** A developer labels about 50 references without seeing the model's labels. The agreement is reported next to every score.
- **A second opinion on a sample, if the first checks raise doubt.** A different model labels a few hundred references, and the agreement with Qwen is reported. This is optional and not planned.
- **A model favouring its own kind.** 162 references were written by a Qwen model. The share of each tier is compared per source model and for the references written by people.
- **Sense checks from the other signals.** Blocking labels should be more common among Code Defect and Security Vulnerability than among Maintainability. References that were acted on should not sit mostly in a tier the labels call unimportant without an explanation.
- **Results that use no labeller.** The same comparison is reported by category alone and on the references written by people alone.

**Why label errors matter less for comparing levels.** One set of labels is used for every level and run. A mislabelled reference is mislabelled for all of them, so it moves the absolute score more than the difference between two levels.

## 8. Limits known now

- **Another model judgement.** The score rests on the severity labels, made by the reviewer's own model. Section 7 bounds that.
- **Blind to volume and to findings outside the references.** A real problem the benchmark lacks earns nothing, and a false alarm costs nothing. This was accepted when the rating of findings was dropped (Section 13).
- **Small tiers.** Splitting 38 references (the five PRs tested so far) into three tiers leaves a handful in each. Tier results need the 30-PR set or the whole benchmark.
- **Run-to-run noise** (Section 14.3 of the per-hunk document) is not reduced by a new score. Repeated runs are still needed.
- **It may not separate the levels.** A blocking problem that depends on other code is where context should matter most. That is a reason to look there, not a result.
- **"Acted on" is unknown for many comments:** those written by an LLM, and those of PRs with no commit after the reviewed one.

## 9. Order of work

| # | Step | State on November 15 |
|---|---|---|
| 1 | Agree on the rubric, the equation and the starting values | Done |
| 2 | Pilot: label the 38 references of the five PRs and re-score the finished per-hunk tests | Done (Section 12) |
| 3 | Rate the validity of the reviewer's findings | Tried on the pilot and dropped (Section 13) |
| 4 | A developer labels about 50 references without seeing the model's labels; agreement is measured | Not done |
| 5 | Label all references with Qwen3.8-27B, thinking on | Not started |
| 6 | Compute "acted on" for the references written by people | Not started |
| 7 | The preference check of Section 5.1 | Not started |
| 8 | Use the score as the selector's reward, with token cost subtracted | Not started |

## 10. Open decisions

1. Whether the category and evidence multipliers stay at 1, or which values they take.
2. Where the label file lives. It is small and every score depends on it, so committing it to the repository is proposed; `data/` is git-ignored today.
3. Whether a developer labels a sample independently (step 4 of Section 9), which is the only way to report how well the model's labels agree with a person.

## 11. What is built

| Path | Content |
|---|---|
| `src/review_outcomes/severity.py` | The rubric text, the labelling prompt, reading a reply, `label_references` |
| `src/review_outcomes/score.py` | The score: `Params`, the five weightings, `score_rows` (per PR and level), `summarize` (per level) |
| `scripts/outcome/label_severity.py` | Labels the reference comments of a per-hunk profile's PRs |
| `scripts/outcome/score_runs.py` | Scores a finished run from its saved files; calls no model |
| `tests/test_review_score.py` | Tests for reading replies, labelling and the score |

**How the pieces behave.**

- **The code shown to the labeller** is the input of the hunk the comment sits on, at `H03_file` (the PR's title and description, the hunk, and its file). On the five PRs all 38 references sit on a hunk; the prompts are 4,000 to 30,000 characters.
- **Not shown to the labeller:** the comment's author and source model. A test checks this.
- **A reply that cannot be read is recorded as an error,** never as a guessed tier, and is sent again by the next run.
- **Saved once.** Labels go to `data/aacr_hunk/labels/severity_<model>.jsonl`, with `_think` in the name when thinking is on. A later run keeps what was made with the same prompt and model settings.
- **A reference without a label is left out of every sum** and counted, so a partly labelled file cannot pass as complete.

**How to run it** (the first needs the model server):

```bash
OUTCOME_PROFILE=cloud-hunk .venv/bin/python scripts/outcome/label_severity.py --thinking
```

```bash
.venv/bin/python scripts/outcome/score_runs.py data/aacr_hunk/outcomes/qwen3.8-27b_aacr-hunk_v2 --labels data/aacr_hunk/labels/severity_qwen3.8-27b_think.jsonl
```

The labelling script takes `--dry-run`, which prints what would be sent.

**Not built:** the category and evidence multipliers have parameters but no command-line switch; "acted on" is not computed; the preference check does not exist.

## 12. Pilot on the five PRs (November 15, 2026)

### 12.1 The labels

Qwen3.8-27B labelled the 38 reference comments twice, with thinking off and with thinking on. All 76 replies could be read. The files are `data/aacr_hunk/labels/severity_qwen3.8-27b.jsonl` and `severity_qwen3.8-27b_think.jsonl`; `severity_check.csv` beside them lists every comment with both labels and reasons.

| Tier | Thinking off | Thinking on |
|---|---|---|
| Blocking | 7 | 2 |
| Should fix | 15 | 17 |
| Nit | 16 | 19 |

- **The two runs agree on 30 of the 38.** In all 8 disagreements the thinking-on label is one tier milder: 5 go from blocking to should fix, 3 from should fix to nit.
- **Sense check against the category:** with thinking off, all 11 Maintainability references are nits and all 7 blocking labels are Code Defect.
- **In two of the disagreements the thinking-on reason says the reference comment is partly wrong** (a metric that does not exist in the code; an error that is never masked). The rubric asks for the severity of the problem if the comment is right, so there the model went beyond its task.

**Decided on November 15, 2026:** the thinking-on labels are used, and reference comments are always labelled with thinking on from now on. The developer reviewed the 8 disagreements and accepted the thinking-on set as a whole. That is an acceptance of the model's labels, not a second set of labels made without seeing them, so no agreement rate between a person and the model can be reported from it.

**The three-tier rubric is kept.** More tiers were considered; the spread over the three tiers gives no reason to split one.

### 12.2 The finished runs, per tier

References covered, with the thinking-on labels: 2 blocking, 17 should fix, 19 nit. "Flat" is plain recall; "agreed" is the score with the weights 5, 2 and 0.5.

Second per-hunk test (thinking off, the current levels):

| Level | Blocking | Should fix | Nit | Flat | Agreed | Steep |
|---|---|---|---|---|---|---|
| `H01_min` | 1 of 2 | 12 of 17 | 8 of 19 | 0.553 | 0.617 | 0.604 |
| `H02_function` | 1 of 2 | 10 of 17 | 8 of 19 | 0.500 | 0.542 | 0.539 |
| `H03_file` | 2 of 2 | 12 of 17 | 8 of 19 | 0.579 | 0.710 | 0.766 |
| `H04_callees1` | 2 of 2 | 11 of 17 | 6 of 19 | 0.500 | 0.654 | 0.721 |
| `H05_callers1` | 2 of 2 | 11 of 17 | 7 of 19 | 0.526 | 0.664 | 0.727 |
| `H06_tests1` | 2 of 2 | 11 of 17 | 8 of 19 | 0.553 | 0.673 | 0.734 |
| `H07_agent` | 1 of 2 | 11 of 17 | 5 of 19 | 0.447 | 0.551 | 0.552 |
| `H08_depth2` | 2 of 2 | 9 of 17 | 6 of 19 | 0.447 | 0.579 | 0.656 |
| `H09_depth2_docs` | 2 of 2 | 9 of 17 | 5 of 19 | 0.421 | 0.570 | 0.649 |
| `H10_all` | 2 of 2 | 10 of 17 | 4 of 19 | 0.421 | 0.598 | 0.675 |
| `H11_pr_summary` | 2 of 2 | 9 of 17 | 8 of 19 | 0.500 | 0.598 | 0.669 |

Reviewer with thinking on (two levels):

| Level | Blocking | Should fix | Nit | Flat | Agreed | Steep |
|---|---|---|---|---|---|---|
| `H01_min` | 1 of 2 | 14 of 17 | 9 of 19 | 0.632 | 0.701 | 0.675 |
| `H03_file` | 2 of 2 | 14 of 17 | 11 of 19 | 0.711 | 0.813 | 0.851 |

First per-hunk test, where the levels from 4 on showed the other hunks as a diff: `H03_file` covers 2 of 2 blocking and 12 of 17 should-fix references (agreed 0.729); every level from `H04_pr_diff` on covers 1 of 2 blocking and 6 or 7 of 17 should-fix (agreed 0.38 to 0.44).

**What this shows, and what it cannot.**

- **Much of the fall in plain recall at the wide levels is nits.** From `H01_min` to `H10_all` the nits covered go from 8 to 4, the should-fix references from 12 to 10, and the blocking ones from 1 to 2. Under the agreed weights `H10_all` (0.598) is close to `H01_min` (0.617), where plain recall has it clearly lower (0.421 against 0.553).
- **`H03_file` is the best level under every weighting.**
- **The blocking tier holds two references,** so "1 of 2" against "2 of 2" is one comment. The blocking-only weighting and the steep one cannot be read on five PRs.
- **Run-to-run noise applies here as it does to plain recall.** One run per level cannot separate these levels.


## 13. Rating the reviewer's findings: tried and dropped (November 15, 2026)

The first version of the score had two more terms: a credit for valid findings that match no reference, and a penalty for speculative or wrong ones. Both need each unmatched finding rated as valid, speculative or wrong. On the five PRs about 80% of the findings match no reference.

**What was run.** Qwen3.8-27B, thinking off, rated the unmatched findings of the second per-hunk test, shown the same input the reviewer had seen. A thinking-on rating of two levels was started for comparison and stopped at 75 of 174 when the decision below was made; nothing of it was kept.

| Level | Unmatched findings | Valid | Speculative | Wrong | Unreadable |
|---|---|---|---|---|---|
| `H01_min` | 89 | 32 | 57 | 0 | 0 |
| `H03_file` | 85 | 43 | 37 | 4 | 1 |

Over all eleven levels: 972 unmatched findings (682 distinct), of which about 4% were rated wrong.

**What the full score looked like** for those two levels, with the weights 5, 2, 0.5 and 0.5 for both the credit and the penalty: `H01_min` 33.0 for covered references, +23.8 for valid findings outside them, −28.5 for noise, 28.2 in all; `H03_file` 38.0, +36.2, −20.5, 53.8 in all.

**Why it was dropped.**

1. **The only rater available is the reviewer's own model.** It rated almost none of its own findings wrong. A misreading that produced a finding is likely to be repeated when rating it.
2. **The term it feeds was as large as the term measured against the benchmark.** The credit for findings outside the references (+24 to +36, and up to +69 for the thinking-on reviewer) rivalled the credit for covered references (33 to 44), so the score would have rested mostly on that rater.
3. **"Speculative" depended on what the rater was shown.** Given the reviewer's own input, the rater could confirm less at `H01_min` than at `H03_file`. Part of the difference between levels was then the rater seeing more, not the reviewer being more right.
4. **Dividing by the references' importance no longer gave a share.** With the outside credit added, the reported score went above 1.

**Decided on November 15, 2026:** unmatched findings are not rated, and the score keeps only its first term (Section 4). The score's purpose is to give severe and important reference comments more weight, which needs no rater.

**What is given up.** A real problem that the benchmark lacks earns nothing, and a false alarm costs nothing. Precision stays in the tables as the only measure that reacts to the number of findings, with its known weakness that it counts every unmatched finding as wrong.

**What remains on disk:** the thinking-off ratings, in `ratings/` under the run folders `qwen3.8-27b_aacr-hunk_v2` and `qwen3.8-27b-think_aacr-hunk_v2`. No code reads them. The rating script and the two extra terms were removed from the code; they are in the branch's history at `23b9284`.

