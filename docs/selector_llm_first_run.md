# Small-LLM context selector, first run on the 15% pilot (November 20, 2026)

**Status: three ways of using a small code LLM as the upgrade selector (zero-shot prompting, frozen embeddings, LoRA fine-tuning) were run on the same 15% per-hunk outcome table as the first selector run (28 PRs, 20 repositories, 397 hunks, 190 references). None beats the tabular logistic baseline or always `H03_file`. Every AUC interval includes 0.5. The best AUC is the frozen embedding at 0.600 [0.47, 0.77], but at the 70% token budget it scores 0.669, below the random-upgrade 95th percentile. Fine-tuning varies more between seeds (AUC 0.465 to 0.612) than the methods differ from each other. Only the tabular baseline beats the random 95th percentile at 70%, and only barely. The run used small models (0.5B and 1.5B) on a laptop GPU; a larger-model version for an A100 is ready but has not been run.**

This document summarizes the run saved with its outputs in `notebooks/selector_llm_colab.ipynb`. Result files are in `results/selector_llm/`. It is the small-LLM follow-up to [selector_first_run.md](selector_first_run.md) and asks the same question as that run's Selector B: which hunks should be upgraded from `H01_min` to `H03_file` within a token budget?

## 1. Setup

| Item | Value |
|---|---|
| Data | per-hunk outcome table from `docs/per_hunk_review_test.md` (Qwen3.8-27B, AACR file-level judge), 15% of AACR-Bench; same files as the first selector run (sha256 in `llm_manifest.json`) |
| Size | 28 PRs, 20 repositories, 397 hunks, 190 references (47 written by a person) |
| Label | `H03_file` covers more severity-weighted references than `H01_min` for this hunk (weights 5 / 2 / 0.5): 37 of 397 hunks |
| Selector | rank hunks by score, upgrade from `H01_min` to `H03_file` in that order until the token budget is used |
| Validation | 5 folds grouped by repository (`GroupKFold`), out-of-fold scores only; positives per test fold: 10, 3, 3, 7, 14 |
| Scoring | the same as the first run: union of covered references per PR, severity-weighted score (`agreed`), tokens as a share of always `H03_file` |
| Locked test set | none |
| Environment | MacBook M2, 16 GB, MPS; Python 3.11, torch 2.15.0, transformers 5.23.0, peft 0.22.0, scikit-learn 1.9.1 |

Methods:
- **Zero-shot**: `Qwen2.5-Coder-1.5B-Instruct` is asked whether the whole file would help review this hunk; the score is log P("Yes") − log P("No"). No training.
- **LLM embedding**: mean-pooled last hidden states of `Qwen2.5-Coder-0.5B-Instruct`, PCA to 32 dimensions inside each fold, logistic regression. **LLM embedding + tabular** adds the 104 tabular features.
- **LLM fine-tuned (LoRA)**: `Qwen2.5-Coder-0.5B-Instruct` with a one-output head, LoRA r = 16 on all attention and MLP projections, class-weighted loss, 3 epochs, learning rate 2e-4, effective batch 16 (1 × 16 accumulation steps on the M2, float32, gradient checkpointing), 3 seeds combined by averaging ranks.
- **Baselines**: tabular logistic regression (the first run's best Selector B), random upgrade order (1,000 orders), cheapest first, always `H01_min`, always `H03_file`, and the hindsight ranking (an upper bound, not a method).

The LLM reads the hunk's `H01_min` text cut to 1,024 tokens: a line of input-side facts (path, language, sizes, how many tokens the whole file adds), then the diff, then the PR text. Nothing from the references, the reviews or the judge enters a feature or the text.

On the M2, fine-tuning took about 20 minutes per fold (15 folds), zero-shot about 6 minutes and the embeddings about 1.5 minutes.

## 2. Checks

All built-in checks passed: the outcome table matches `coverage.csv` for every PR and level, the fixed-level counts match per-hunk doc Section 19.1, and cheapest first matches Section 20.3 (108, 111, 114, 117, 117). Re-running the notebook from its caches reproduced every result file byte for byte.

## 3. Can a model tell which hunks need the file? (AUC)

Out-of-fold AUC of the upgrade label, with a repository-clustered bootstrap 95% interval (2,000 resamples):

| Method | AUC | 95% interval |
|---|---|---|
| LLM embedding | **0.600** | [0.47, 0.77] |
| tabular logistic | 0.544 | [0.44, 0.68] |
| zero-shot LLM | 0.542 | [0.39, 0.69] |
| LLM fine-tuned (LoRA), rank average of 3 seeds | 0.539 | [0.43, 0.71] |
| LLM embedding + tabular | 0.489 | [0.39, 0.58] |
| cheapest first (no learning) | 0.350 | [0.25, 0.45] |

- Every learned interval includes 0.5. With 37 positive hunks the run cannot separate a weak signal from none.
- Fine-tuning by seed: AUC 0.494, 0.612 and 0.465 (mean 0.523, sd 0.064). The spread between seeds is larger than the differences between methods.
- The zero-shot model answers "Yes" for all 397 hunks, so its score is only the size of its preference for Yes. That ranking is no better than the tabular features.
- Adding the embedding to the tabular features makes the model worse than either alone: 136 features against 37 positives overfit.
- Cheapest first is clearly below 0.5: the hunks whose file adds the most tokens are more likely to gain. It still does well on count at a fixed budget because cheap upgrades let it upgrade many hunks.

## 4. At a fixed token budget

Target (per-hunk doc Section 20.1): 115 or more of 190 references at 70% of the tokens of always `H03_file` or less. Always `H01_min` covers 106 (score 0.602, 26% of the tokens); always `H03_file` covers 116 (score 0.708). Random upgrade at 70%: mean 112.9, score mean 0.666, 95th percentile 0.702.

At 70%, sorted by score:

| Policy | Covered | Score | Human recall | AI recall | Beats random p95 | Meets target |
|---|---|---|---|---|---|---|
| hindsight (upper bound) | 129 | 0.750 | 0.511 | 0.734 | yes | yes |
| **tabular logistic** | 113 | **0.708** | 0.404 | 0.657 | yes | no |
| zero-shot LLM | 111 | 0.698 | 0.404 | 0.643 | no | no |
| LLM fine-tuned (LoRA) | 113 | 0.696 | 0.426 | 0.650 | no | no |
| cheapest first (no learning) | 117 | 0.681 | 0.383 | 0.692 | no | yes |
| LLM embedding | 114 | 0.669 | 0.426 | 0.657 | no | no |
| LLM embedding + tabular | 113 | 0.662 | 0.383 | 0.664 | no | no |

- Only the tabular baseline beats the random 95th percentile, by 0.006. It matches always `H03_file` in score (0.708) at 70% of its tokens, as in the first run.
- The embedding has the best AUC, but its score stays between 0.669 and 0.675 from 50% to 85% of the tokens, below zero-shot and fine-tuning at 70%. AUC counts every positive hunk the same; the score weights blocking references and depends on which hunks fit in the budget.
- Fine-tuned, each seed at 70%: 110, 116 and 112 references (scores 0.679, 0.708, 0.642).
- Reaching 115 references: cheapest first at 70% (117), the embedding at 60% (116); above the target budget, cheapest first and fine-tuning at 85% (117).
- The hindsight ranking covers 129 references at 40% of the tokens, so the budget is not what limits a selector; the ranking is.

Curves for all budgets: `results/selector_llm/frontier_llm.png`. Full table: `results/selector_llm/llm_budget_table.csv`.

## 5. Paired bootstrap at 70%

Score difference against the comparison policy, resampling repositories, 95% interval:

| Method | vs always `H03_file` | vs cheapest first | vs tabular logistic |
|---|---|---|---|
| zero-shot LLM | −0.010 [−0.040, 0.014] | +0.017 [−0.029, 0.078] | −0.010 [−0.028, 0.014] |
| LLM embedding | −0.038 [−0.118, 0.015] | −0.012 [−0.086, 0.037] | −0.038 [−0.112, 0.018] |
| LLM embedding + tabular | −0.046 [−0.122, 0.008] | −0.019 [−0.085, 0.028] | −0.046 [−0.112, 0.004] |
| LLM fine-tuned (LoRA) | −0.012 [−0.032, 0.004] | +0.015 [−0.025, 0.076] | −0.012 [−0.032, 0.016] |

Every interval includes zero. No LLM method can be told apart from always `H03_file`, cheapest first or the tabular baseline. All point estimates against `H03_file` and the tabular baseline are negative.

## 6. Human and AI references, tiers

Covered at 70% (from `llm_groups.csv`):

| Policy | Human (47) | AI (143) | Blocking (15) | Should fix (65) | Nit (110) |
|---|---|---|---|---|---|
| always `H01_min` | 17 | 89 | 8 | 45 | 53 |
| always `H03_file` | 20 | 96 | 12 | 48 | 56 |
| tabular logistic | 19 | 94 | 12 | 49 | 52 |
| zero-shot LLM | 19 | 92 | 12 | 48 | 51 |
| LLM embedding | 20 | 94 | 10 | 48 | 56 |
| LLM embedding + tabular | 18 | 95 | 10 | 47 | 56 |
| LLM fine-tuned (LoRA) | 20 | 93 | 12 | 47 | 54 |
| cheapest first | 18 | 99 | 11 | 46 | 60 |

Human-written references stay hard for every policy (at most 20 of 47, 43%). The embedding methods lose two blocking references, which explains most of their lower scores. Cheapest first gains its count on nits.

## 7. What this means

1. On this pilot a small code LLM (0.5B to 1.5B) does not choose better hunks to upgrade than 104 input-side numbers in a logistic regression. All three uses of the LLM land within the noise of the tabular baseline, of always `H03_file` and of each other.
2. The result agrees with the first selector run: the signal in the input side is weak (AUC about 0.55 to 0.6), and 37 positive hunks from 20 repositories are too few to tell methods apart. Fine-tuning shows this most clearly: the seeds disagree more than the methods do.
3. The gap to the hindsight ranking (129 against 113 references at 70%) shows there is room for a better ranking, but this run gives no sign that the hunk text, read by a small LLM, holds it.
4. The numbers are from the five methods as designed, with no tuning on these results, but there is no locked test set and only one reviewer run.

Differences from the first selector run: the folds are 5 groups of repositories instead of leave one repository out, so the tabular baseline's numbers differ a little (AUC 0.544 here against 0.548; at 70%, 113 references and score 0.708 against 114 and 0.710).

## 8. Next steps (not decided)

- `notebooks/selector_llm_colab_a100.ipynb` runs the same pipeline with larger models (3B for embeddings and LoRA, 7B for zero-shot), 2,048-token inputs and bfloat16 on an A100. It changes model size and input length together; to tell which helps, a run that changes one of them is needed.
- More positives matter more than a larger model: rerun on the full benchmark when its per-hunk outcome table exists, with a locked test set.
- Whether the LLM should see the `H03_file` context itself rather than only the hunk and its size.

## 9. Files

- Notebook with outputs: `notebooks/selector_llm_colab.ipynb`
- A100 version (not yet run): `notebooks/selector_llm_colab_a100.ipynb`
- Results: `results/selector_llm/`
  - `llm_upgrade_auc.csv`: AUC and intervals per method
  - `llm_budget_table.csv`: every method at every budget
  - `llm_bootstrap_comparisons.csv`: paired bootstrap at 70%
  - `llm_groups.csv`: covered references by human/AI, tier, context label and category
  - `random_upgrade_by_budget.csv`: the random-upgrade distribution
  - `llm_oof_scores.csv`: every hunk's out-of-fold score per method and seed, with the label
  - `frontier_llm.png`: quality-cost curves
  - `llm_manifest.json`: inputs with sha256, settings, versions
  - `cache/`: zero-shot scores, embeddings and per-fold fine-tuning scores; a rerun with the same settings loads these instead of recomputing
