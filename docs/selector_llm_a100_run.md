# Small-LLM context selector with larger models on an A100 (November 20, 2026)

**Status: the small-LLM selector of [selector_llm_first_run.md](selector_llm_first_run.md) was rerun on a Colab A100 with larger models: 3B for embeddings and LoRA, 7B for zero-shot, and 2,048-token inputs instead of 1,024. The run used the same 15% pilot (28 PRs, 20 repositories, 397 hunks, 37 positive). Because the runs' folds differ, every method was re-scored on common folds, including the 20 folds of the [first selector run](selector_first_run.md) (Section 6).**

**No LLM method, at either size, beats that run's tabular logistic selector: 114 references, score 0.710 at 70% of the `H03_file` tokens. Nor does any beat its tree models on AUC (about 0.60 to 0.62). On the M2 run's 5 folds, the 3B embedding reaches AUC 0.636 [0.55, 0.73], the only interval above 0.5. On the first run's 20 folds it drops to 0.523, so that lead depends on the folds. The one result that holds on every fold set is negative: the 7B zero-shot model selects significantly worse than the tabular baseline and always `H03_file`. LoRA at 3B is no better than at 0.5B.**

The run is saved with its outputs in `notebooks/selector_llm_colab_a100.ipynb`, and its result files are in `results/selector_llm_results_a100/`. The comparisons on common folds are made by `scripts/analysis/compare_selector_llm_runs.py`:
- on the M2 run's folds: `results/selector_llm_results_a100/same_folds/` (Sections 2 to 5);
- on the first selector run's folds: `results/selector_llm_results_a100/loo_folds/` (Section 6).

## 1. Setup and what changed

The question, label, scoring, budgets and checks are those of the first small-LLM run (its Section 1). The inputs have the same sha256, and all built-in checks passed. Only these settings changed:

| Item | M2 run | A100 run |
|---|---|---|
| Zero-shot model | `Qwen2.5-Coder-1.5B-Instruct` | `Qwen2.5-Coder-7B-Instruct` |
| Embedding and LoRA model | `Qwen2.5-Coder-0.5B-Instruct` | `Qwen2.5-Coder-3B-Instruct` |
| Input length | 1,024 tokens | 2,048 tokens |
| Fine-tuning precision and batch | float32, 1 × 16 accumulation steps | bfloat16 autocast, 4 × 4 accumulation steps (effective batch 16 in both) |
| Hardware | MacBook M2, MPS | NVIDIA A100-SXM4-80GB |
| Software | Python 3.11, scikit-learn 1.9.1, pandas 3.1.1, torch 2.15.0, transformers 5.23.0, peft 0.22.0 | Python 3.13, scikit-learn 1.6.1, pandas 2.2.3, torch 2.14.1+cu130, transformers 5.22.1, peft 0.21.2 |

The LoRA settings were otherwise the same: r = 16, 3 epochs, learning rate 2e-4, 3 seeds averaged by rank. Fine-tuning took 157 to 180 seconds per fold on the A100, 43 minutes for all 15 folds. The final notebook run loaded the zero-shot scores and embeddings from its cache.

Model size and input length changed together, so this run cannot say which of the two made a difference.

## 2. The runs used different folds

Both small-LLM notebooks use `GroupKFold(5)` by repository, but scikit-learn 1.6.1 and 1.9.1 assign repositories to folds differently. Positives per test fold were [10, 3, 3, 7, 14] on the M2 and [10, 2, 3, 8, 14] on the A100. The tabular baseline uses no LLM, yet its AUC moved from 0.544 to 0.562 for this reason alone; its scores across the two runs have a Spearman correlation of 0.88. The first selector run used yet another scheme: leave one repository out, 20 folds.

So the runs' result files should not be compared row by row. `scripts/analysis/compare_selector_llm_runs.py` executes the M2 notebook's setup cells with the chosen fold scheme. It then re-scores the cached model outputs on those folds:
- The zero-shot scores need no folds, so they are unchanged.
- The embedding methods are refit from the cached 0.5B and 3B embeddings.
- LoRA cannot be refit without retraining, so both LoRA runs keep their own folds. Their rows are marked as such.

As checks, the script must reproduce the M2 run's tabular and 0.5B embedding scores exactly on the M2 folds. On the 20 folds it must reproduce the first run's tabular logistic AUC (0.548). Both checks pass. On the 20 folds it also reproduces the first run's 114 references and score 0.710 at 70%.

Sections 3 to 5 are on the M2 folds, unless marked otherwise.

## 3. AUC (M2 folds)

Out-of-fold AUC of the upgrade label, repository-clustered bootstrap 95% interval (2,000 resamples):

| Method | AUC | 95% interval |
|---|---|---|
| LLM embedding 3B | **0.636** | [0.55, 0.73] |
| LLM embedding 0.5B | 0.600 | [0.47, 0.77] |
| LLM embedding + tabular 3B | 0.584 | [0.51, 0.67] |
| zero-shot 7B | 0.579 | [0.46, 0.71] |
| tabular logistic | 0.544 | [0.44, 0.68] |
| LoRA 3B (A100 folds) | 0.542 | [0.40, 0.73] |
| zero-shot 1.5B | 0.542 | [0.39, 0.69] |
| LoRA 0.5B | 0.539 | [0.43, 0.71] |
| LLM embedding + tabular 0.5B | 0.489 | [0.39, 0.58] |
| cheapest first (no learning) | 0.350 | [0.25, 0.45] |

- **Embedding, 3B.** On the two 5-fold splits, the 3B embedding is the only method whose interval excludes 0.5: 0.636 on the M2 folds and 0.631 on the A100 folds. On the first run's 20 folds it falls to 0.523 (Section 6), so the lead does not hold across fold schemes. Its out-of-fold scores in the A100 run have a Spearman correlation of only 0.47 with the 0.5B embedding's in the M2 run. The larger model ranks the hunks differently, not just more sharply.
- **Embedding + tabular.** With 3B embeddings this method no longer falls below either part alone (0.584, against 0.489 at 0.5B). It is still below the embedding alone.
- **Zero-shot.** The 7B model answers "Yes" for 71% of hunks; the 1.5B model answered "Yes" for all of them. Its AUC rises from 0.542 to 0.579.
- **LoRA.** Fine-tuning gains nothing from the larger model: 0.542 against 0.539. Per seed, AUC is 0.476, 0.581 and 0.504 (sd 0.044, against 0.064 at 0.5B). The rank-averaged scores of the two runs have a Spearman correlation of −0.31: the two fine-tuned models agree on nothing.

## 4. At a fixed token budget (M2 folds)

The target is unchanged: 115 or more of 190 references at 70% of the tokens of always `H03_file`. For comparison, always `H03_file` covers 116 references (score 0.708). A random upgrade order at 70% has a mean score of 0.666, with a 95th percentile of 0.702.

At 70%, sorted by score:

| Policy | Covered | Score | Human recall | AI recall | Beats random p95 | Meets target |
|---|---|---|---|---|---|---|
| hindsight (upper bound) | 129 | 0.750 | 0.511 | 0.734 | yes | yes |
| **tabular logistic** | 113 | **0.708** | 0.404 | 0.657 | yes | no |
| zero-shot 1.5B | 111 | 0.698 | 0.404 | 0.643 | no | no |
| LoRA 0.5B | 113 | 0.696 | 0.426 | 0.650 | no | no |
| LoRA 3B (A100 folds) | 114 | 0.692 | 0.426 | 0.657 | no | no |
| LLM embedding + tabular 3B | 113 | 0.685 | 0.340 | 0.678 | no | no |
| cheapest first (no learning) | 117 | 0.681 | 0.383 | 0.692 | no | yes |
| LLM embedding 3B | 114 | 0.675 | 0.426 | 0.657 | no | no |
| LLM embedding 0.5B | 114 | 0.669 | 0.426 | 0.657 | no | no |
| LLM embedding + tabular 0.5B | 113 | 0.662 | 0.383 | 0.664 | no | no |
| zero-shot 7B | 112 | 0.637 | 0.383 | 0.657 | no | no |

Score at every budget:

| Method | 40% | 50% | 60% | 70% | 85% |
|---|---|---|---|---|---|
| tabular logistic | 0.637 | 0.681 | **0.704** | **0.708** | 0.708 |
| LLM embedding 3B | 0.644 | 0.662 | 0.656 | 0.675 | 0.692 |
| LLM embedding + tabular 3B | 0.623 | 0.642 | 0.671 | 0.685 | **0.708** |
| zero-shot 7B | 0.635 | 0.631 | 0.623 | 0.637 | 0.650 |
| zero-shot 1.5B | **0.662** | 0.667 | 0.665 | 0.698 | 0.700 |
| LoRA 3B (A100 folds) | 0.650 | 0.667 | 0.679 | 0.692 | 0.692 |
| LoRA 0.5B | 0.604 | 0.629 | 0.623 | 0.696 | 0.704 |
| cheapest first | 0.623 | 0.629 | 0.635 | 0.681 | 0.704 |

- **Tabular baseline.** It is still the only method that beats the random 95th percentile at 70%.
- **The 3B embedding.** On these folds it covers the most references at small budgets: 113 at 40% and 116 at 50% and 60%, against 109 to 111 for the tabular baseline. Its weighted score stays lower, though, because it misses blocking references. On the A100 folds it covers 10 of the 15 at 70%, against 12 for always `H03_file`. On the first run's 20 folds the count advantage also goes away: 110 to 112 references from 40% to 60%.
- **The 7B zero-shot model.** It has the second-best AUC here but the worst score at every budget from 50% on. Its scores do not depend on the folds. At 70% it covers 9 of the 15 blocking references; always `H03_file` covers 12 and the tabular baseline 11. AUC treats every positive hunk the same, but the score depends on whether the few hunks with blocking references are upgraded early.
- **Embedding + tabular, 3B, on the A100 folds.** There it reached 119 references and score 0.702 at 70%, the best count of any learned method. On the M2 folds the same method gets 113 and 0.685, and on the first run's folds 109 and 0.683. That number was fold luck.

Full tables: `results/selector_llm_results_a100/same_folds/budget.csv` (M2 folds) and `results/selector_llm_results_a100/llm_budget_table.csv` (A100 folds). The A100 run's curves are in `frontier_llm_a100.png`.

## 5. Paired bootstrap at 70% (M2 folds)

The table gives the score difference against each comparison policy, resampling repositories, with the 95% interval:

| Method | vs always `H03_file` | vs cheapest first | vs tabular logistic |
|---|---|---|---|
| zero-shot 1.5B | −0.010 [−0.040, 0.014] | +0.017 [−0.029, 0.078] | −0.010 [−0.028, 0.014] |
| **zero-shot 7B** | **−0.071 [−0.159, −0.008]** | −0.044 [−0.117, 0.013] | **−0.071 [−0.147, −0.007]** |
| LLM embedding 0.5B | −0.038 [−0.118, 0.015] | −0.012 [−0.086, 0.037] | −0.038 [−0.112, 0.018] |
| LLM embedding 3B | −0.033 [−0.104, 0.013] | −0.006 [−0.063, 0.036] | −0.033 [−0.097, 0.015] |
| LLM embedding + tabular 0.5B | −0.046 [−0.122, 0.008] | −0.019 [−0.085, 0.028] | −0.046 [−0.112, 0.004] |
| LLM embedding + tabular 3B | −0.023 [−0.089, 0.014] | +0.004 [−0.030, 0.034] | −0.023 [−0.084, 0.017] |
| LoRA 0.5B | −0.012 [−0.032, 0.004] | +0.015 [−0.025, 0.076] | −0.012 [−0.032, 0.016] |
| LoRA 3B (A100 folds) | −0.015 [−0.074, 0.024] | +0.012 [−0.055, 0.078] | −0.015 [−0.063, 0.008] |

The 7B zero-shot model is the only method that differs from a baseline beyond chance, and it differs for the worse. Every other interval includes zero.

## 6. All three selector runs on the first run's folds

The [first selector run](selector_first_run.md) trained non-LLM selectors on the same 397 hunks: tabular, language-only and TF-IDF features with logistic regression, random forests, boosted trees and an MLP. It used leave one repository out (20 folds). Leave one group out does not depend on the scikit-learn version, so the LLM methods were re-scored on those exact folds. The first run's rows below are taken from its document, and the tabular logistic row is reproduced exactly.

| Method | Run | AUC [95% interval] | Covered at 70% | Score at 70% |
|---|---|---|---|---|
| boosted trees, tabular + text | first | **0.616** [0.51, 0.76] | – | – |
| boosted trees, tabular | first | 0.599 [0.52, 0.71] | – | – |
| random forest, tabular + text | first | 0.598 [0.52, 0.74] | – | – |
| random forest, tabular | first | 0.597 [0.51, 0.75] | – | – |
| zero-shot 7B | A100 | 0.579 [0.46, 0.71] | 112 | 0.637 |
| LLM embedding 0.5B | M2 | 0.574 [0.43, 0.75] | 112 | 0.642 |
| **tabular logistic (Selector B)** | first | 0.548 [0.45, 0.67] | 114 | **0.710** |
| LoRA 3B (A100 folds) | A100 | 0.542 [0.40, 0.73] | 114 | 0.692 |
| zero-shot 1.5B | M2 | 0.542 [0.39, 0.69] | 111 | 0.698 |
| LoRA 0.5B (M2 folds) | M2 | 0.539 [0.43, 0.71] | 113 | 0.696 |
| LLM embedding + tabular 3B | A100 | 0.525 [0.43, 0.65] | 109 | 0.683 |
| LLM embedding 3B | A100 | 0.523 [0.41, 0.70] | 109 | 0.677 |
| LLM embedding + tabular 0.5B | M2 | 0.518 [0.43, 0.62] | 112 | 0.677 |
| language only, all models | first | 0.40 to 0.47 | – | – |
| cheapest first (no learning) | – | 0.350 [0.25, 0.45] | 117 | 0.681 |
| always `H03_file` | – | – | 116 | 0.708 |
| hindsight, `H01_min` to `H03_file` only | – | – | 129 | 0.750 |

The first run's document reports budget results only for its best configurations, which is why the tree models have no budget numbers here. Its hindsight row (133 references, score 0.804) chooses among four levels, including `H05_callers1`. It is not comparable to the upgrade-only hindsight above.

Paired bootstrap at 70% on these folds (`loo_folds/bootstrap.csv`):
- **Against the tabular logistic selector, every LLM method has a negative point estimate.**
- **Two intervals exclude zero:**
  - zero-shot 7B: −0.073 [−0.148, −0.011];
  - LLM embedding + tabular 3B: −0.027 [−0.074, −0.002].
- **Against always `H03_file`, only the 7B zero-shot model excludes zero:** −0.071 [−0.159, −0.008].

## 7. Which model is best

1. **The best selector across all three runs is the first run's tabular logistic regression (Selector B).** It covers 114 references, score 0.710, at 70% of the tokens. No LLM method, at 0.5B, 1.5B, 3B or 7B, scores higher at the budget on any fold set.
2. **The best ranking signal by AUC comes from the first run's tree models on tabular features** (about 0.60 to 0.62). The LLM methods land between 0.52 and 0.64, depending on the folds. The 3B embedding's 0.636 holds on both 5-fold splits but falls to 0.523 on 20 folds. The small LLMs read the hunk text and found no more signal than 104 input-side numbers.
3. **Larger models did not help as a selector:**
   - **Zero-shot (1.5B → 7B):** the 7B model is the one result that holds on every fold set. It is significantly worse than both the tabular selector and always `H03_file`.
   - **Embedding (0.5B → 3B):** the gain depends on the folds.
   - **LoRA (0.5B → 3B):** no gain.
4. **The limit is the data, not the model.**
   - With 37 positives from 20 repositories, results move with the fold scheme, the scikit-learn version and the seed by as much as the methods differ.
   - The hindsight ranking (129 references at 40% of the tokens) shows the room is in the ranking.
   - More positive hunks, from the full benchmark's per-hunk outcome table, matter more than a larger model.

The numbers come from the methods as designed, with no tuning on these results. The first run's rows are the best of many configurations picked on the same data, so they are optimistic. There is no locked test set and only one reviewer run.

## 8. Next steps (not decided)

- **Fix the folds for all selector notebooks.** Use leave one repository out, or save the fold assignment to a file, so runs on different machines and scikit-learn versions share folds.
- **Separate the two changes.** Run 3B at 1,024 tokens, or 0.5B at 2,048, to tell model size apart from input length. This is only worth doing if a signal holds across fold schemes.
- **Rerun on the full benchmark** when its per-hunk outcome table exists, with a locked test set. Freeze the method choice, for now the tabular logistic selector, before looking at test results.

## 9. Files

- **Notebook with outputs:** `notebooks/selector_llm_colab_a100.ipynb`
- **Results (A100 folds):** `results/selector_llm_results_a100/`, laid out like `results/selector_llm/`:
  - `llm_upgrade_auc.csv`, `llm_budget_table.csv`, `llm_bootstrap_comparisons.csv`, `llm_groups.csv`
  - `random_upgrade_by_budget.csv`, `llm_oof_scores.csv`, `frontier_llm_a100.png`, `llm_manifest.json`
  - `cache/`: 7B zero-shot scores, 3B embeddings, per-fold LoRA scores
- **Comparisons on common folds**, each with `auc.csv`, `budget.csv` and `bootstrap.csv`:
  - `results/selector_llm_results_a100/same_folds/`: on the M2 run's folds
  - `results/selector_llm_results_a100/loo_folds/`: on the first selector run's folds
  - Both are made by `scripts/analysis/compare_selector_llm_runs.py`. To rerun it, run from the project root with the cs536 environment and `SELECTOR_DATA` set to the data folder.
