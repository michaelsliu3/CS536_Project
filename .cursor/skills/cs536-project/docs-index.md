# Project documents

Read the original file in `docs/`. New files added there become official sources automatically.

| File | Use when |
|------|----------|
| `docs/MLII_fall2026_project_kickoff.pdf` | Course values, allowed project types, compute/data rules, grading, proposal/midpoint/final requirements, team rules |
| `docs/context_selection_proposal_detailed.docx` | Current proposal: learned per-diff context selection, ContextCRBench, frozen Qwen reviewer, accuracy-cost evaluation, leakage rules, timeline, success criteria |

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
