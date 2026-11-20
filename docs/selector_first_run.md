# Selector training, first run on the 15% pilot (November 19, 2026)

**Status: the first learned selectors have been trained and evaluated offline on the 15% per-hunk outcome table (28 PRs, 20 repositories, 397 hunks, 190 references). None beats always `H03_file` on quality. The best one, an upgrade ranking with logistic regression on tabular features, matches always `H03_file` at 70% of its tokens: 0.710 versus 0.708, a difference of +0.002 with 95% interval [−0.029, 0.025]. It covers 114 of 190 references, one short of the 115 target. A rule with no learning, cheapest first, covers 117 at the same budget. The predictive signal is weak: the best upgrade AUC is about 0.6, and a control that sees only the programming language comes close. All configurations were picked from many on the same data and no test set was held out, so every number here is optimistic.**

This document summarizes the Colab run saved in `notebooks/selector_training_colab_run_15pct.ipynb`, with outputs. The code is in `notebooks/selector_training_colab.ipynb`. The run used the version of the notebook at commit `6457794`, which reads the full `aacr_hunk_15%` folder. The current version (`3809d9c`) reads the same data from a slim folder of three files and gives the same results (checked locally, full mode).

## 1. Setup

| Item | Value |
|---|---|
| Data | per-hunk outcome table from `docs/per_hunk_review_test.md` (Qwen3.8-27B, AACR file-level judge), 15% of AACR-Bench |
| Size | 28 PRs, 20 repositories, 397 hunks, 190 references (15 blocking, 65 should fix, 110 nit; 47 written by a person) |
| Candidate levels | `H01_min`, `H03_file`, `H05_callers1`, `H10_all` |
| Score | severity-weighted recall, weights 5 / 2 / 0.5 (`agreed`); a PR's covered references are the union over its hunks at the chosen levels |
| Validation | leave one repository out (20 folds), out-of-fold predictions only; input-side features only |
| Seeds | 0, 1, 2 (mean reported) |
| Locked test set | off for this pilot (it would hold 72 of the 190 references) |
| Environment | Colab, numpy 2.4.6, pandas 2.2.3, scikit-learn 1.6.1 |

Selectors:
- **A, gain regression**: predict each level's weighted gain, then choose `argmax pred − λ·tokens/1000`.
- **B, upgrade ranking**: classify whether `H03_file` adds weighted references over `H01_min`, then upgrade the top-ranked hunks until the token budget is used.
- **C, direct classification**: predict the hindsight-best level at a fixed λ.

Feature sets: `tabular` (104 input-side features: hunk size, file kind, trace counts, per-level token and item counts), `language` (language only, as a shortcut control), `text` (TF-IDF of the `H01_min` text, fitted inside each fold), and `tabular+text`.

## 2. Checks

All built-in checks passed:
- the outcome table matches `coverage.csv` for every PR and level;
- the fixed-level covered counts match Section 19.1 of the per-hunk doc (106, 106, 116, 111, 109, 107, 99, 96, 99, 94);
- cheapest first matches Section 20.3 (108, 111, 114, 117, 117);
- the random-upgrade means are within 0.3 of that section.

## 3. Baselines

| Policy | Covered / 190 | Score | Human recall | AI recall | Token share vs H03 |
|---|---|---|---|---|---|
| always `H01_min` | 106 | 0.602 | 0.362 | 0.622 | 0.26 |
| always `H03_file` | 116 | 0.708 | 0.426 | 0.671 | 1.00 |
| always `H05_callers1` | 109 | **0.729** | 0.404 | 0.629 | 2.16 |
| always `H10_all` | 94 | 0.631 | 0.362 | 0.538 | 10.75 |
| best fixed level, chosen in-fold | 107 | 0.708 | 0.383 | 0.622 | 2.12 |
| random level from the four | 106.8 | 0.666 | 0.387 | 0.619 | 3.54 |

More context does not help on average: always `H10_all` uses 10.8 times the tokens of `H03_file` and covers 22 fewer references.

Only 37 of 397 hunks gain weighted references when upgraded from `H01_min` to `H03_file`. 249 hunks cover no reference at any level.

## 4. Can a model tell which hunks need the file? (Selector B AUC)

Out-of-fold AUC of the upgrade label, with a repository-clustered bootstrap 95% interval:

| Features | Model | AUC (mean ± sd over seeds) | 95% interval |
|---|---|---|---|
| tabular+text | boosted trees | 0.616 | [0.51, 0.76] |
| tabular | boosted trees | 0.599 | [0.52, 0.71] |
| tabular+text | random forest | 0.598 ± 0.016 | [0.52, 0.74] |
| tabular | random forest | 0.597 ± 0.007 | [0.51, 0.75] |
| tabular | MLP | 0.593 ± 0.084 | [0.58, 0.74] |
| tabular | logistic | 0.548 | [0.45, 0.67] |
| language | all models | 0.40 to 0.47 | — |

The signal is weak but above chance: most tabular intervals sit just above 0.5. The order of the top models differs slightly from a local run with scikit-learn 1.9.1, where tabular+text random forest was first (0.627). The MLP varies a lot between seeds.

The features the best ranking model leans on most (permutation importance) are whether the path is a test file, the hunk's new line count, lines added, the size of the `H02_function` context, and second-hop caller counts.

## 5. At a fixed token budget

Target (per-hunk doc Section 20.1): 115 or more references at 70% of the tokens of always `H03_file` or less.

At 70%, sorted by score (top rows):

| Policy | Covered | Score | Human recall | Beats random p95 | Meets target |
|---|---|---|---|---|---|
| hindsight (upper bound, 55% tokens) | 133 | 0.804 | 0.574 | yes | yes |
| **B, tabular, logistic** | 114 | **0.710** | 0.404 | yes | no |
| B, language, logistic | 113 | 0.702 | 0.426 | no | no |
| B, tabular+text, logistic | 112 | 0.700 | 0.383 | no | no |
| B, language, random forest | 118 | 0.688 | 0.404 | no | yes |
| cheapest first (no learning) | 117 | 0.681 | 0.383 | no | yes |
| random upgrade (mean of 1,000) | 112.9 | 0.666 | — | — | — |
| best A (language, MLP, 64% tokens) | 111.3 | 0.662 | 0.411 | no | no |

- Only one learned configuration beats the random-upgrade 95th percentile (0.702) in score, and only barely.
- The configurations that meet the 115 count are the language-only control, a text MLP, and cheapest first. Count and score disagree because the score weights blocking references.
- Selector A, the roadmap's preferred target, does worse than B. Its best configuration uses language only.
- Selector C collapses toward `H01_min`: the hindsight label is `H01_min` for 334 to 374 of 397 hunks. Its best score is 0.667, for example language-only logistic regression at λ = 0.3: 110 references at 0.73 times the H03 tokens.

## 6. Paired bootstrap against the baselines

Score difference at the selector's budget, clustered by repository, 95% interval:

| Selector | Versus | Difference | 95% interval |
|---|---|---|---|
| B, tabular, logistic (70%) | always `H03_file` | +0.002 | [−0.029, 0.025] |
| | always `H01_min` | +0.108 | [0.020, 0.218] |
| | cheapest first | +0.029 | [−0.018, 0.088] |
| A, language, MLP (64%) | always `H03_file` | −0.038 | [−0.093, 0.006] |
| | always `H01_min` | +0.067 | [−0.008, 0.182] |
| | cheapest first | −0.012 | [−0.079, 0.059] |

The only clear difference is over always `H01_min`. The best selector cannot be told apart from always `H03_file` or from cheapest first.

## 7. Human and AI references

The professor asked for these to be reported separately.

| Policy | Human (47) | AI (143) | Blocking (15) |
|---|---|---|---|
| always `H01_min` | 17 | 89 | 8 |
| always `H03_file` | 20 | 96 | 12 |
| always `H05_callers1` | 19 | 90 | 14 |
| A @ 70% | 19 | 89 | 11 |
| B @ 70% | 19 | 95 | 12 |

Human-written references are much harder for every policy: at most 20 of 47 (43%), against up to 96 of 143 (67%) for AI references.

## 8. What this means

1. On this pilot, a learned selector saves about 30% of the `H03_file` tokens without losing quality. It does not improve quality, and cheapest first does about as well with no learning.
2. The input-side features carry little signal (AUC about 0.6), and a language-only model comes close. With 37 positive hunks from 20 repositories, the run cannot separate a weak signal from noise.
3. The best configurations were picked after looking at all of them on the same data, so the numbers above are optimistic.

Open questions for the team:
- Rerun on the full benchmark when its outcome table exists, with `LOCK_TEST=True`, freezing every choice before looking at test results.
- Whether to add features that can see the context itself (for example, embeddings of the `H03_file` text) rather than only its size.
- Whether to keep Selector A as the main target, given that B is simpler and does better here.

Later: small-LLM selectors (zero-shot, embeddings, LoRA; 0.5B to 7B) were compared with this run on its 20 folds in [selector_llm_a100_run.md](selector_llm_a100_run.md) Section 6. None beats Selector B with tabular logistic regression.

## 9. Files

- Run with outputs: `notebooks/selector_training_colab_run_15pct.ipynb`
- Current code: `notebooks/selector_training_colab.ipynb`
- Result CSVs and `frontier.png`: written to `MyDrive/CS536/selector_results/` (not in git)
