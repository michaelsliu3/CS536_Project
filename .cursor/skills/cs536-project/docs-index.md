# Project documents

Read **every original document under `docs/` recursively** before working in this
repository, as required by root `AGENTS.md`. This index is a navigation aid, not
an exhaustive inventory or a substitute for the originals. New documents must
also be read; distinguish official guidance from historical handoffs.

| File | Context provided |
|------|----------|
| `docs/report_format/README.md` and `docs/report_format/neurips_2026/` | User-selected NeurIPS 2026 report format: original LaTeX template, style and checklist; supersedes writing skills’ IEEE defaults while course page limits still apply |
| `docs/MLII_fall2026_project_kickoff.pdf` | Course values, allowed project types, compute/data rules, grading, proposal/midpoint/final requirements, team rules |
| `docs/initial_investigation_context.md` | Historical investigation, professor feedback, decisions, and handoff context; verify historical observations against current sources |
| `docs/context_selection_proposal_detailed.docx` | Current proposal: learned per-diff context selection, ContextCRBench, frozen Qwen reviewer, accuracy-cost evaluation, leakage rules, timeline, success criteria |
| `docs/outcome_table_first_run.md` | First outcome table (Nov 14, 2026): 1,850 cells reviewed by Qwen3.8-27B, the malformed replies and their cause, the retry rule and its result, the 41 cells left out, the judge run, and the first per-level results (recall about 6 to 7% at every level; judge not yet validated). Section 11 records three later checks: how few files the reviewer comments on, the AACR-Bench paper's own prompt for plain LLMs, and AACR-Bench's v2 reference set with the table re-scored on it |
| `docs/per_hunk_review_test.md` | Per-hunk review test on the branch `per-hunk-review` (started Nov 15, 2026): why it exists, the decision and timeline, the eleven per-hunk levels, the AACR-Bench paper's own reviewer prompt and tag reply format, how it is scored, what was checked on five PRs, the cost estimate, what is open, and the first test on five PRs (Section 12: 47 to 63% recall at the first three levels against 5 to 16% per PR; from `H04_pr_diff` on the reviewer comments on the other hunks, which led to the changes of Section 13: other hunks out of the chain, a marker for lines the PR changes elsewhere, a summary level at the end, and only findings on the reviewed hunk counted; and the second test of Section 14: every level at 42 to 58% recall and 13 to 17% precision, with level differences inside the run-to-run noise; Section 15: thinking on raises recall to 24 and 27 of 38 at two levels with lower precision and about ten times the output; Section 16: why the levels do not differ and what recall and precision miss; Section 17: three runs of all ten levels, where `H03_file` is best, every level from `H07_agent` on is no better than no context, and a selector has little room on quality; Section 18: which model step uses thinking; Section 19: all ten levels on 28 PRs with 190 references, 23 of the PRs drawn at random, where `H03_file` has the highest recall at 61%, `H03_file` to `H06_tests1` cannot be told apart, every level from `H07_agent` on covers fewer references than the file level beyond chance, and the levels to keep are not yet chosen; Section 20: a test on the 28 PRs of whether a selector can tell which hunks need the file, from the change, from the reviewer's first review, or from the reviewer's own request, where none does much better than a coin flip and the direction is not decided) |
| `docs/selector_llm_first_run.md` | Small-LLM upgrade selector on the 15% pilot (Nov 20, 2026): zero-shot Qwen2.5-Coder-1.5B, frozen 0.5B embeddings and LoRA fine-tuning, all out of fold on 5 repository-grouped folds; none beats the tabular baseline or always `H03_file`, every AUC interval includes 0.5, fine-tuning seeds differ more than the methods; the A100 run is in `docs/selector_llm_a100_run.md` |
| `docs/selector_llm_a100_run.md` | The same small-LLM selector on an A100 with larger models (7B zero-shot, 3B embeddings and LoRA, 2,048-token inputs; Nov 20, 2026). The runs' folds differ, so every method is re-scored on common folds, including the first selector run's 20 folds (Section 6 compares all three selector runs). No LLM method beats that run's tabular logistic selector (114 references, score 0.710 at 70%) or its tree models' AUC; the 3B embedding's AUC 0.636 on 5 folds falls to 0.523 on 20; the 7B zero-shot model is significantly worse than the tabular selector and always `H03_file` on every fold set; LoRA gains nothing |
| `docs/review_score_design.md` | Design of a severity-weighted review score, on the branch `review-score` (started Nov 15, 2026): why recall and precision fall short, the signals (severity labels, category, written by a person, acted on; AACR's v2 membership is not used), the score's three parts, the few numbers that are choices and how they are set and checked, the labelling plan and its checks; Section 12: the pilot on five PRs; Section 13: rating the reviewer's unmatched findings, tried and dropped, so the score is severity-weighted recall only; Section 14: the 190 labels of the 28-PR run (15 blocking, 65 should fix, 110 nit) and what the score changed in the reading |

Re-list `docs/` if the folder may have changed. Prefer the newest dated proposal, report, or instructor note when sources disagree.

## Kickoff milestone checklists

**Proposal (1 page, due Mon Sep 28, 5 pts)**

- One-sentence research question; what yes and no answers look like
- Motivation and 3–5 papers actually read
- Data, compute, method, and whether access exists today
- Evaluation: metrics, baselines, ablations, leakage plan
- Main risk and fallback
- Week-by-week plan, roles, anticipated LLM use

**Midpoint (≤ 3 pages, ~Mon Nov 2, 10 pts)**

- Initial results, lessons, direction changes, next steps
- GitHub link
- A negative result is fine. No baseline is not.

**Final report + talk (5–8 pages NeurIPS-style + unlimited appendix, ~Tue Dec 1 / talks Dec 3, 7, 10, 40 pts)**

- Motivation, methods, results, experiments, observations, lessons
- GitHub link
- Every teammate presents and answers questions

## Proposal phases (implementation order)

1. Audit ContextCRBench and establish the frozen-reviewer pilot
2. Generate and cache offline outcomes for context subsets
3. Fine-tune the compact context selector
4. Compare accuracy-cost frontiers with fixed and adaptive baselines
5. Run error/ablation analysis; cross-reviewer transfer is optional

## Starting references from the proposal

1. Hu et al. (2025), ContextCRBench
2. Li et al. (2022), CodeReviewer
3. Wu et al. (2024), Repoformer
4. Xu et al. (2024), RECOMP
5. Qwen2.5-Coder and a CodeBERT-style selector
