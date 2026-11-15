# Review score design (branch `review-score`, started November 15, 2026)

**Status: the equation and its starting values were agreed on November 15, 2026, and the code for the pilot is written and tested (Section 11). No comment has been labelled and no model has been called for it yet. The branch `review-score` was created from `per-hunk-review` to hold it.**

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

Three parts, each answering one question.

1. **Did the review find what matters?** Each reference comment has an importance. A covered reference earns its importance times a match grade.
2. **Did it find real problems the benchmark lacks?** Each finding that matches no reference is rated valid, speculative or wrong, and given a severity by the same rubric. A valid one earns part of its severity weight.
3. **How much noise did it make?** Each speculative or wrong finding costs a fixed penalty.

```
importance(reference) = severity weight × category multiplier × evidence multiplier

score = Σ importance × match grade              over the references
      + outside credit × Σ severity weight      over valid findings that match no reference
      − noise penalty × (speculative + wrong findings)
```

With the starting values of Section 5 the multipliers are 1 and a match is yes or no, so the score is:

```
score = Σ severity weight of the covered references
      + 0.5 × Σ severity weight of valid findings that match no reference
      − 0.5 × (speculative + wrong findings)
```

**An example.** A PR has three references: one blocking (5), one should-fix (2), one nit (0.5). The review covers the blocking one and the nit, adds one valid should-fix finding that is not among the references, and makes three speculative or wrong findings.

| Part | Value |
|---|---|
| Covered references | 5 + 0.5 = 5.5 |
| Valid finding outside the references | 0.5 × 2 = 1 |
| Noise | 0.5 × 3 = 1.5 |
| Score | 5.5 + 1 − 1.5 = 5 |
| Score as reported | 5 ÷ (5 + 2 + 0.5) = 0.67 |

- **For reporting,** the score of a PR is divided by the summed importance of its references, so a PR with many references does not dominate.
- **For the selector,** the same sum is taken per hunk without dividing. A hunk without references then scores by its valid findings and its noise alone, which is the only outcome that can be measured for most hunks.
- **Token cost** is not part of this score. The selector's reward subtracts it separately.

**What is always reported next to the score,** so that no conclusion rests on the weights:

- recall per severity tier, with blocking recall first;
- speculative and wrong findings per hunk;
- valid findings that match no reference, per severity tier;
- plain recall and precision as before.

## 5. The numbers that are choices

Every signal has a number, and the numbers are few on purpose. Three are neutral at the start.

| Choice | Meaning | Starting value |
|---|---|---|
| Severity weights | Worth of a blocking, should-fix and nit comment | 5, 2, 0.5 |
| Category multiplier | Extra weight for a category, on top of severity | 1 for every category (neutral) |
| Evidence multiplier | Extra weight for a reference written by a person, and more if it was acted on | 1 (neutral); the first value to try is 1.25 and 1.5 |
| Match grade for a related remark | Credit when a finding is on the same code and related, but not the same problem | 1 for the same problem only (neutral), because the current judge answers yes or no. A graded judge would use 0.5 |
| Outside credit | Share of its severity weight that a valid finding outside the references earns | 0.5 |
| Noise penalty | Cost of one speculative or wrong finding | 0.5 |

**The starting values were agreed on November 15, 2026.** The methods of Section 5.1 check them. A value changes only through those methods, with the date and the reason recorded here, and never to make a comparison come out a certain way.

### 5.1 How the numbers are set

**They are parameters of the measure, not of a model.** A model's hyperparameters are tuned to raise a result. These numbers define what the result means, so tuning them until the levels separate would fit the measure to the answer that is hoped for. They are set from what a developer values, written down with a date, and then left alone.

Four methods, used together:

1. **Exchange-rate questions.** A developer answers a few direct questions, and the answers fix the ratios.
   - How many nits are worth one blocking problem found? (With 5 and 0.5, ten.)
   - How many should-fix problems are worth one blocking problem? (With 5 and 2, two and a half.)
   - How many false alarms is one blocking problem worth reading through? (With 5 and a penalty of 0.5, ten.)
2. **A check against a person's preferences.** Take about 40 pairs of reviews of the same hunk, from different levels or runs. A developer picks the better review of each pair without seeing the score. The share of pairs where the score agrees is reported. A model fitted to half of the choices (a logistic regression on the difference between the two reviews' counts per tier and noise) shows which weights the choices imply, and the other half tests them.
3. **Several weightings instead of one.** Every comparison is reported under a small fixed set of weightings, and a conclusion counts as robust only if it holds under all of them.

   | Name | Blocking, should fix, nit |
   |---|---|
   | Flat | 1, 1, 1 (plain recall) |
   | Linear | 3, 2, 1 |
   | Starting values | 5, 2, 0.5 |
   | Steep | 25, 5, 1 |
   | Blocking only | 1, 0, 0 |

   The neutral multipliers are varied the same way (for example Security Vulnerability at 1.5, evidence at 1.25 and 1.5).
4. **A rule that needs no weights.** If one level covers at least as many references in every tier as another and makes no more noise, it is better under any weights that respect the order of the tiers. Where this holds, the weights do not matter; where it does not, the answer depends on them and the write-up says so.

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

**Decided on November 15, 2026: Qwen3.8-27B does both jobs,** on the cloud server. It labels the reference comments by severity, and it rates the reviewer's unmatched findings (valid, speculative or wrong, with a severity). No Claude model is used for either.

| Job | What the model is given | Known weakness of using the reviewer's own model |
|---|---|---|
| Severity of the reference comments | The comment and the code it is about | 162 references were written by another Qwen model (Qwen-Coder-480B), and it may rate those higher. One set of labels serves every level and run, so a bias moves all of them alike |
| Validity of the reviewer's findings | The finding, the hunk and the level's context | The findings were written by this same model. A misreading that produced a wrong finding is likely to be repeated when rating it, so the share of valid findings may come out too high. Comparisons between levels are affected less than the absolute numbers |

The option not taken was a Claude model through the plan's included usage: stronger and independent of the reviewer, at no API bill, but that route has never been run. It stays available as a check on a sample (Section 7).

## 7. How the labels are checked

A label from a model changes the score, so it is treated like the judge: an input that must be validated.

- **Agreement with a person.** A developer labels about 50 references without seeing the model's labels. The agreement is reported next to every score.
- **A second opinion on a sample, if the first checks raise doubt.** A different model labels or rates a few hundred items, and the agreement with Qwen is reported. This is optional and not planned for the pilot.
- **A model favouring its own kind.** 162 references were written by a Qwen model. The share of each tier is compared per source model and for the references written by people.
- **The rater's leniency toward its own findings.** A developer rates a sample of unmatched findings without seeing the model's ratings, and the agreement is reported. Every table that uses the ratings says that the rater is the reviewer's own model.
- **Sense checks from the other signals.** Blocking labels should be more common among Code Defect and Security Vulnerability than among Maintainability. References that were acted on should not sit mostly in a tier the labels call unimportant without an explanation.
- **Results that use no labeller.** The same comparison is reported by category alone and on the references written by people alone.

**Why label errors matter less for comparing levels.** One set of labels is used for every level and run. A mislabelled reference is mislabelled for all of them, so it moves the absolute score more than the difference between two levels.

## 8. Limits known now

- **Another model judgement.** The score rests on the severity labels and on the validity ratings of findings. Section 7 bounds the first; the second has the same weakness as the judge and needs the same check.
- **Small tiers.** Splitting 38 references (the five PRs tested so far) into three tiers leaves a handful in each. Tier results need the 30-PR set or the whole benchmark.
- **Run-to-run noise** (Section 14.3 of the per-hunk document) is not reduced by a new score. Repeated runs are still needed.
- **It may not separate the levels.** A blocking problem that depends on other code is where context should matter most. That is a reason to look there, not a result.
- **A graded match needs a new judge prompt,** which is no longer AACR-Bench's judge. Until that is decided the match grade stays yes or no.
- **"Acted on" is unknown for many comments:** those written by an LLM, and those of PRs with no commit after the reviewed one.

## 9. Order of work

Steps 1 and 2 are prepared: the code exists (Section 11), and no model has been called.

1. Agree on the rubric and the starting values (this document). The equation and the starting values are agreed; the rubric's wording has not been reviewed line by line.
2. Pilot: Qwen3.8-27B labels the 38 references of the five PRs, a developer checks them, and the finished per-hunk tests are re-scored per tier. The labelling needs the cloud server; the re-scoring does not, because the judge's matches are saved.
3. Rate the validity and severity of the findings of those tests.
4. A developer labels about 50 references; agreement is measured.
5. Label all references with Qwen3.8-27B.
6. Compute "acted on" for the references written by people.
7. The preference check of Section 5.1.
8. Use the score as the selector's reward, with token cost subtracted.

## 10. Open decisions

1. Whether the three questions of Section 5.1 are answered before the pilot, to confirm the agreed starting values.
2. Whether the labeller and the rater run with thinking on. Thinking made the reviewer about 8.5 times slower; for 1,506 short labelling calls that is hours, not days.
3. Whether the judge is changed to grade matches.
4. Where the label file lives. It is small and every score depends on it, so committing it to the repository is proposed; `data/` is git-ignored today.
5. Whether a speculative finding costs as much as a wrong one.

## 11. What is built

| Path | Content |
|---|---|
| `src/review_outcomes/severity.py` | The rubric text, the two prompts, reading a reply, `label_references` and `rate_findings` |
| `src/review_outcomes/score.py` | The score: `Params`, the five weightings, `score_rows` (per PR and level), `summarize` (per level) |
| `scripts/outcome/label_severity.py` | Labels the reference comments of a per-hunk profile's PRs |
| `scripts/outcome/rate_findings.py` | Rates the findings of a run that the judge matched to no reference |
| `scripts/outcome/score_runs.py` | Scores a finished run from its saved files; calls no model |
| `tests/test_review_score.py` | Nine tests, including the worked example of Section 4 |

**How the pieces behave.**

- **The code shown to the labeller** is the input of the hunk the comment sits on, at `H03_file` (the PR's title and description, the hunk, and its file). On the five PRs all 38 references sit on a hunk; the prompts are 4,000 to 30,000 characters.
- **Not shown to the labeller:** the comment's author and source model. A test checks this.
- **A reply that cannot be read is recorded as an error,** never as a guessed tier, and is sent again by the next run.
- **Saved once.** Labels go to `data/aacr_hunk/labels/severity_<model>.jsonl`, with `_think` in the name when thinking is on. Ratings go to the run folder, `ratings/<hunk>/<level>.json`. A later run keeps what was made with the same prompt and model settings.
- **Only unmatched findings are rated,** and identical inputs once. The second per-hunk test has 972 unmatched findings over its eleven levels, 682 of them distinct; its `H01_min` and `H03_file` have 174.
- **Without ratings** the scoring script reports recall per tier and weighted recall under the five weightings. With ratings it adds the valid findings outside the references, the noise and the score.
- **A reference without a label is left out of every sum** and counted, so a partly labelled file cannot pass as complete.

**How to run the pilot** (the first two need the model server):

```bash
OUTCOME_PROFILE=cloud-hunk .venv/bin/python scripts/outcome/label_severity.py
```

```bash
OUTCOME_PROFILE=cloud-hunk .venv/bin/python scripts/outcome/rate_findings.py
```

```bash
.venv/bin/python scripts/outcome/score_runs.py data/aacr_hunk/outcomes/qwen3.8-27b_aacr-hunk_v2 --labels data/aacr_hunk/labels/severity_qwen3.8-27b.jsonl
```

Both model scripts take `--dry-run`, which prints what would be sent, and `--thinking`.

**Not built:** the category and evidence multipliers have parameters but no command-line switch; "acted on" is not computed; the judge still answers yes or no; the preference check does not exist.

