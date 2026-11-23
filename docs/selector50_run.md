# Context selectors on the AACR 50% set: non-LLM and LLM side by side (November 23, 2026)

**Status:** 21 selectors were trained and scored out of fold on the AACR 50% set: 93 PRs, 37 repositories, 928 hunks and 684 references, at the four kept levels of [aacr_50pct_run.md](aacr_50pct_run.md).
- 8 selectors use no LLM and ran on a laptop.
- 13 read the hunk with Qwen2.5-Coder, from 0.5B to 14B, on a Colab A100.

All use the same label, folds, budgets and scoring, so they share one set of tables.

**Result: no selector beats choosing hunks at random at any budget, so always `H02_function` remains the practical choice.**
- **The one signal that stands out** comes from the zero-shot models at 7B and 14B, which need no training: AUC 0.591 [0.538, 0.642] and 0.592 [0.538, 0.640]. These are the only intervals above 0.5.
- **That signal does not carry to a budget.** At the main budget the 7B model scores 0.578 and the 14B model 0.559, against 0.587 for the 95th percentile of random choice.
- **Going from 7B to 14B adds nothing.** No model under 7B beats the tabular logistic baseline (AUC 0.551), and LoRA fine-tuning is the weakest LLM family.

Where things are:

| What | Where |
|---|---|
| Non-LLM notebook (laptop, about 8 minutes) | `notebooks/selector50_nonllm.ipynb`, results in `results/selector50_nonllm/` |
| LLM notebook | `notebooks/selector50_llm_a100.ipynb` |
| The full LLM run on a Colab A100 80 GB | `notebooks/selector50_llm_a100v2.ipynb`, results in `results/selector50_llm_results/` |
| An earlier session of the same run on a T4, with only the models up to 3B | `notebooks/selector50_llm_a100v1.ipynb` |
| All 21 selectors in one set of tables (Sections 3 to 6) | `results/selector50_combined/`, written by `scripts/analysis/rescore_selector50_cache.py` |

## 1. Setup

**Question.** Every hunk is reviewed at `H01_min`. Under a token budget, which hunks should be upgraded to a wider level? A selector ranks the hunks, and hunks are upgraded in that order until the budget is spent.

**Label.** A hunk is positive ("wider") when some wider level covers more references than `H01_min`: 190 of 928 hunks.

**Folds.** 5 folds of whole repositories. They are built by a plain-Python rule, so both notebooks get the same folds on any scikit-learn version:
- repositories go largest first, with ties broken by a seeded hash;
- each one goes to the fold with the fewest hunks.

This gives 186/186/186/185/185 hunks and 36/46/28/37/43 positives per fold. Both notebooks check the fold hash (`1778fb4a7e`) and the inputs' sha256.

**Budget.** A selector spends a share (10% to 100%) of the extra tokens that upgrading every hunk would cost. The main comparison is the upgrade to `H02_function` at 30%. Quality is the severity score with the agreed weights 5/2/0.5. The two references without a severity label get weight 0.

**Baselines.**

| Baseline | What it does |
|---|---|
| Fixed levels | Every hunk at one level |
| Random upgrade | 1,000 random orders; a selector must beat their 95th percentile (p95) |
| Cheapest first | Upgrades the hunks with the fewest extra tokens first; no learning |
| Hindsight | Knows each hunk's outcome; an upper bound, not achievable |

**Statistics.**
- AUC, with a 95% interval from a bootstrap over repositories.
- A paired, repository-clustered bootstrap for differences at the main budget.
- For two non-LLM models, a shuffled-label control (20 repeats).

**Selectors.**

| Family | Models | How it scores a hunk |
|---|---|---|
| Tabular | Logistic regression | 114 input-side facts (sizes, counts, language, path kind, context tokens per level). The pre-declared baseline |
| Non-LLM | Logistic, random forest (500 trees) and boosted trees, on four feature sets: tabular, text (TF-IDF of the path and the `H01_min` text), tabular + text, and language only | Trees use 3 seeds and 64 SVD dimensions of the text |
| Zero-shot | 1.5B, 7B, 14B | log P(Yes) − log P(No) to "would more code help the reviewer find real problems?"; no training |
| Embedding | 0.5B, 1.5B, 3B, 7B, each alone and with the tabular facts | Mean-pooled last hidden state → PCA(32) inside each fold → logistic regression |
| LoRA | 0.5B, 3B, 3 seeds each | One-output classification head, r = 16, class-weighted loss, 3 epochs; seeds averaged by rank |

Every LLM reads the same text, cut to 2,048 tokens:
- one line of input-side facts;
- the diff;
- the PR's title and description.

## 2. How the results were put together

The LLM notebook fits its model lists to the GPU Colab gives it, and caches every model output on Drive per model, seed and fold.

The run took several sessions:
- Earlier sessions computed the models up to 7B and most LoRA 3B folds.
- One session got a T4 and could only re-score the models up to 3B (`selector50_llm_a100v1.ipynb`).
- The last session got an A100 80 GB (`selector50_llm_a100v2.ipynb`). It computed the 14B zero-shot scores (212 s) and the last LoRA 3B fold (451 s). Everything else it read from the cache.

That session's tables hold all 13 LLM selectors and the tabular baseline. It had no copy of the non-LLM scores.

`scripts/analysis/rescore_selector50_cache.py` produces the combined tables of Sections 3 to 6:
- It executes the LLM notebook's cells with every model the cache holds.
- It reads each model output from the cache. No model runs; only the tokenizers are loaded, to rebuild the texts and check the cache key.
- It merges the non-LLM notebook's out-of-fold scores.

**Checks.**
- All 17 AUC rows and all 336 budget rows of the A100 session's own tables are reproduced (largest AUC difference 7e-6, budget scores identical).
- The tabular baseline equals the non-LLM notebook's to 1e-6.
- The non-LLM notebook was run twice. The second run reproduced every table, with out-of-fold scores equal to within floating-point rounding (2e-16).

The 7B LoRA was not run.

## 3. Fixed levels and the room for a selector

| Policy | Covered (of 684) | Score | Tokens vs `H01_min` |
|---|---|---|---|
| Always `H01_min` | 342 | 0.544 | 1.00× |
| Always `H02_function` | **363** | 0.600 | 1.93× |
| Always `H03_file` | 357 | 0.603 | 3.44× |
| Always `H04_callees1` | 356 | 0.603 | 5.56× |
| Hindsight, `H02_function` at 30% | 412 | 0.675 | 1.28× |

These are the numbers of [aacr_50pct_run.md](aacr_50pct_run.md). The room is large:
- A selector that knew each hunk's outcome would cover 49 more references than always `H02_function`, with a third fewer tokens.
- Wider context matters most for two groups:
  - blocking references: 21 of 38 covered at `H01_min`, 29 at `H03_file`;
  - references that need repository context: 51 of 97, and 60 of 97.

## 4. Can a selector tell which hunks need context? (AUC)

| Method | Family | AUC [95% CI] |
|---|---|---|
| **zero-shot 14B** | LLM, no training | **0.592 [0.538, 0.640]** |
| **zero-shot 7B** | LLM, no training | **0.591 [0.538, 0.642]** |
| embedding + tabular 7B | LLM | 0.573 [0.525, 0.628] |
| embedding + tabular 3B | LLM | 0.557 [0.507, 0.613] |
| zero-shot 1.5B | LLM, no training | 0.555 [0.495, 0.599] |
| embedding 7B | LLM | 0.555 [0.491, 0.624] |
| embedding + tabular 1.5B | LLM | 0.554 [0.504, 0.605] |
| embedding + tabular 0.5B | LLM | 0.554 [0.501, 0.611] |
| tabular + text, logistic | non-LLM | 0.552 [0.499, 0.600] |
| **tabular, logistic** (baseline) | non-LLM | **0.551 [0.498, 0.600]** |
| tabular + text, random forest | non-LLM | 0.549 [0.503, 0.600] |
| tabular + text, boosted trees | non-LLM | 0.542 [0.490, 0.604] |
| tabular, random forest | non-LLM | 0.537 [0.494, 0.583] |
| text, logistic | non-LLM | 0.535 [0.483, 0.578] |
| LoRA 3B | LLM | 0.531 [0.466, 0.605] |
| embedding 3B | LLM | 0.531 [0.472, 0.589] |
| embedding 1.5B | LLM | 0.521 [0.463, 0.576] |
| tabular, boosted trees | non-LLM | 0.514 [0.469, 0.562] |
| LoRA 0.5B | LLM | 0.512 [0.451, 0.571] |
| embedding 0.5B | LLM | 0.504 [0.441, 0.563] |
| language only, logistic | non-LLM | 0.488 [0.425, 0.535] |
| cheapest first to `H02_function` | no learning | 0.425 [0.362, 0.479] |

**Chance level.** With labels shuffled, the tabular logistic model gets AUC 0.502 on average and 0.540 at the 95th percentile. Its real AUC (0.551) is just above that. The boosted trees' AUC (0.514) is not.

**Non-LLM models: more flexible models and the hunk's text do not help.**
- Adding the text to the tabular facts adds 0.001 to the logistic model.
- Random forest and boosted trees do not beat it.

If the facts held a signal that a linear model missed, the trees should have found it.

**LLMs: AUC rises with model size up to 7B, then stops.**

| Family | AUC, smallest to largest |
|---|---|
| Zero-shot | 0.555 (1.5B) → 0.591 (7B) → 0.592 (14B) |
| Embedding | 0.504 (0.5B) → 0.521 (1.5B) → 0.531 (3B) → 0.555 (7B) |
| Embedding + tabular | 0.554 (0.5B) → 0.554 (1.5B) → 0.557 (3B) → 0.573 (7B) |
| LoRA | 0.512 (0.5B) → 0.531 (3B) |

- **Only the 7B and 14B models stand clear of the baseline.** Of these, only the zero-shot intervals exclude 0.5.
- **The 7B and 14B zero-shot models largely agree** (Spearman 0.67 between their scores). Averaging their ranks gives 0.597, so the larger model adds little that the 7B model lacks. That figure was computed after the fact and is not one of the 21 selectors.
- **Both rank on something the tabular facts do not hold.** Their scores correlate only weakly with the tabular model's (Spearman 0.28 for 7B, 0.21 for 14B).
- **How often each model answers Yes:**

  | Model | Answers Yes |
  |---|---|
  | 1.5B | every hunk (only its relative log-odds rank the hunks) |
  | 7B | 72% of hunks |
  | 14B | 81% of hunks |

**Three cautions on the zero-shot result.**
- **Selection.** It is the best of 21 methods on one set of folds, so its AUC is likely somewhat optimistic.
- **The 15% pilot disagrees.** There, with the same model and prompt, the 7B zero-shot model selected significantly worse than the tabular baseline on every fold set ([selector_llm_a100_run.md](selector_llm_a100_run.md)). That pilot had another label set: 37 positives among 397 hunks.
- **AUC is not the budget result.** Neither model beats random choice at a budget (Section 5).

**LoRA fine-tuning is the weakest LLM family.** With 928 hunks and 190 positives, the seeds vary widely:

| Model | AUC per seed | Rank average |
|---|---|---|
| LoRA 0.5B | 0.487, 0.545, 0.482 | 0.512 |
| LoRA 3B | 0.521, 0.541, 0.517 | 0.531 |

Both averages are below the untrained 1.5B zero-shot model's 0.555.

## 5. At a token budget

Upgrade to `H02_function` at 30% of the extra tokens (1.28× the tokens of `H01_min`):

| Method | Covered | Score |
|---|---|---|
| Hindsight (upper bound) | 412 | 0.675 |
| Always `H02_function` (100% of the extra tokens) | 363 | 0.600 |
| Random upgrade, p95 | 361 | **0.587** |
| embedding 7B | 354 | 0.580 |
| cheapest first (no learning) | 351 | 0.580 |
| tabular + text, boosted trees | 356 | 0.579 |
| zero-shot 7B | 358 | 0.578 |
| language only, logistic | 352 | 0.578 |
| LoRA 3B | 352 | 0.565 |
| zero-shot 14B | 347 | 0.559 |
| zero-shot 1.5B | 351 | 0.557 |
| tabular, logistic | 347 | 0.553 |
| embedding + tabular 7B | 347 | 0.551 |
| Random upgrade, mean | 351.6 | 0.567 |
| Always `H01_min` | 342 | 0.544 |

**No learned selector reaches the random p95 at the main budget, for any upgrade level.**

| Upgrade to | Random p95 | Best learned selector |
|---|---|---|
| `H02_function` | 0.587 | 0.580 |
| `H03_file` | 0.588 | 0.582 |
| `H04_callees1` | 0.583 | 0.583 (a tie) |

Across all budgets, only 12 of 378 points pass the random p95 (3%). The points are 21 selectors × 3 upgrade levels × 6 budgets below 100%. Chance alone would give that many, and the 12 are scattered over methods and budgets.

**The zero-shot models across budgets** (upgrade to `H02_function`):

| Share of the extra tokens | 10% | 20% | 30% | 40% | 50% | 70% |
|---|---|---|---|---|---|---|
| zero-shot 7B | 0.567 | 0.580 | 0.578 | 0.582 | 0.583 | 0.586 |
| zero-shot 14B | 0.550 | 0.560 | 0.559 | 0.585 | 0.585 | 0.586 |
| tabular, logistic | 0.559 | 0.562 | 0.553 | 0.559 | 0.575 | 0.580 |
| Random upgrade, p95 | 0.567 | 0.578 | 0.587 | 0.596 | 0.602 | 0.609 |

The 14B model ranks as well as the 7B model by AUC but does worse at the low budgets.

**AUC does not carry over to the budget.**
- The budget score weights references by severity and charges each hunk its tokens. A slightly better ranking of "covers more" does not pick up more of the heavy references.
- The cheapest-first rule ranks hunks worse than chance (AUC 0.425). Yet it scores like the best selectors, because it upgrades many cheap hunks.

**Paired bootstrap, `H02_function` at 30%.** Differences in score, with 95% intervals:

| Method | vs tabular logistic | vs cheapest first | vs always `H02_function` | vs always `H01_min` |
|---|---|---|---|---|
| zero-shot 14B | +0.006 [−0.031, 0.038] | −0.021 [−0.062, 0.018] | −0.041 [−0.102, 0.013] | +0.014 [−0.003, 0.032] |
| zero-shot 7B | +0.025 [−0.018, 0.065] | −0.001 [−0.041, 0.032] | −0.022 [−0.076, 0.024] | +0.034 [−0.004, 0.070] |
| embedding 7B | +0.027 [−0.008, 0.067] | 0.000 [−0.039, 0.043] | −0.020 [−0.066, 0.028] | +0.035 [0.007, 0.071] |
| LoRA 3B | +0.012 [−0.027, 0.049] | −0.014 [−0.054, 0.028] | −0.035 [−0.096, 0.023] | +0.021 [0.004, 0.040] |
| tabular + text, boosted trees | +0.026 [−0.002, 0.059] | −0.001 [−0.033, 0.038] | −0.021 [−0.058, 0.012] | +0.035 [−0.005, 0.079] |
| tabular, logistic | — | −0.027 [−0.067, 0.014] | −0.047 [−0.096, −0.010] | +0.009 [−0.027, 0.046] |

What the intervals show:
- **No selector beats the tabular baseline or the cheapest-first rule.**
- **Against always `H01_min`, the gains come from tokens.** Where a selector beats it, the selector spends 28% more tokens; its ranking is not the reason.
- **Against always `H02_function`, the losses come from tokens.** The tabular selector loses to it, but always `H02_function` spends 1.5 times the selector's tokens.

## 6. Which references the selectors find

Covered references, by group:

| Group | `H01_min` | `H02_function` | `H03_file` | The 21 selectors at 30% of `H02_function` |
|---|---|---|---|---|
| Blocking (38) | 21 | 25 | 29 | 21 to 24 |
| Should fix (197) | 116 | 127 | 119 | 116 to 126 |
| Needs repository context (97) | 51 | 51 | 60 | 45 to 53 |
| Written by a person (181) | 68 | 70 | 75 | 65 to 77 |

No selector, with or without an LLM, finds the hunks whose blocking or repository-level references a wider level would cover.

## 7. What it means

1. **A learned selector does not save tokens on this set.** [project_target.md](project_target.md), Section 3, requires a selector to beat choosing at random and the cheapest-first rule. None of the 21 selectors beats either.
2. **The non-LLM ceiling is the facts, not the model.** Sizes, counts, language, path and the hunk's words give AUC about 0.55, whatever the model. This agrees with the flat learning curve of [aacr_50pct_run.md](aacr_50pct_run.md), Section 4: more data of the same kind is not what is missing.
3. **Reading the code is the one lead, and model size is not the way forward.**
   - An untrained 7B model gives the best ranking. A 14B model matches it and does worse at a budget.
   - Neither beats random choice at a budget.
   - On the 15% pilot, the 7B model's result went the other way.

   Worth testing, with the 7B model:
   - a question closer to what the wider levels add, such as whether the hunk calls code defined elsewhere;
   - folds or data the result was not chosen on.
4. **Fine-tuning is not worth its GPU time at this size.**
   - LoRA took most of the run's GPU time:
     - about 14 minutes per fold for 0.5B and 24 for 3B, going by the cache's file times in the earlier sessions;
     - 451 s for one 3B fold on the A100.
   - It scored below the untrained models.
5. **For the review pipeline, always `H02_function` stays the choice.** It covers 363 references for 1.93× the tokens of no context, and it is the cheapest way to the gain of context ([aacr_50pct_run.md](aacr_50pct_run.md)).

**Corrections to earlier notes.**
- [selector_llm_a100_run.md](selector_llm_a100_run.md), Section 7, said the limit of the pilot selectors was the data. On the 50% set, with 2.3 times the hunks and 5 times the positives, the non-LLM selectors stay at AUC about 0.55. So the limit is in what the facts and the label carry, not in the amount of data.
- The same document found the 7B zero-shot model significantly worse than the tabular baseline. That held on the pilot. It does not hold here.

## 8. Limits

- **One reviewer run, no locked test set.** All numbers are out of fold on the same 928 hunks. The method choice was not pre-registered beyond the tabular baseline.
- **The label is noisy.** It comes from one review per hunk and level, and a wider level loses a reference on 89 to 107 hunks ([aacr_50pct_run.md](aacr_50pct_run.md)). Some of that is the reviewer's run-to-run variation, which no selector can predict.
- **One prompt and one text format.** The zero-shot question and the 2,048-token text were fixed before the run and not tuned.
- **The 7B LoRA was not run.** Given LoRA at 0.5B and 3B, it is unlikely to change the picture.

## 9. How to reproduce

Run these from the project root, in the `cs536` environment.

**Non-LLM selectors** (about 8 minutes on a MacBook M2). This writes `results/selector50_nonllm/`:

```bash
jupyter nbconvert --to notebook --execute --inplace notebooks/selector50_nonllm.ipynb
```

**LLM selectors on Colab.**
1. Open `notebooks/selector50_llm_a100.ipynb`.
2. Put `aacr_hunk_50%_selector_files` on Drive.
3. Run all.

The notebook fits its models to the GPU. Every model, seed and fold is cached on Drive, so a later session continues where one stopped. On an A100 80 GB with an empty cache, expect several hours, most of it LoRA.

**All selectors in one set of tables.** Copy the Drive results folder to `results/selector50_llm_results/`, with its `cache/`, then run:

```bash
python scripts/analysis/rescore_selector50_cache.py
```

This writes `results/selector50_combined/`. The model caches (about 29 MB of `.npy` files) are not committed.
