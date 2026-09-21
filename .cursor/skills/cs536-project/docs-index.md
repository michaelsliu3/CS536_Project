# Project documents

Read the original file in `docs/`. New files added there become official sources automatically.

| File | Use when |
|------|----------|
| `docs/MLII_fall2026_project_kickoff.pdf` | Course values, allowed project types, compute/data rules, grading, proposal/midpoint/final requirements, team rules |
| `docs/ml2_project_proposal_detailed.docx` | Research questions, datasets, model/compute plan, experiment phases, metrics, leakage rules, timeline, success criteria |

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

1. Baseline + stability on CodeReviewer
2. Add context types one at a time
3. Exhaustive minimal-context target on small candidate sets
4. Greedy forward / backward selection; optional attribution
5. Cross-model transfer

## Starting references from the proposal

1. Li et al. (2022), CodeReviewer
2. Microsoft CodeBERT / CodeReviewer dataset
3. Hu et al. (2025), ContextCRBench
4. Qwen2.5-Coder
5. ElliCE as conceptual motivation, not a template to copy
