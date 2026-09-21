# Outline Contract

Target: CS536 / ML II final report, NeurIPS-style, 5-8 pages plus unlimited appendix.

## Section Tree

1. **Introduction** — Open on the deployment problem: the same change gets different review verdicts depending on presentation and supplied context. State the stability-constrained minimal-context objective as the contribution (C1). Preview results. Citation quota 8-10. Figure quota 1 (teaser: one example, two presentations, two verdicts).
2. **Related Work** — Three threads: code-review automation and benchmarks, robustness under semantics-preserving transformations, context selection and compression. Close each thread with what it leaves open. Must surface the ContextCRBench vs SWE-PRBench conflict explicitly. Citation quota 12-18. Figure quota 0.
3. **Problem Formulation** — Define the review decision, the perturbation family, correctness and stability metrics, token cost, and the constrained objective with thresholds tau_c and tau_s. Distinguish our objective from correctness-only minimal sufficient context. Citation quota 4-6. Figure quota 1 (method/pipeline diagram).
4. **Experimental Setup** — Datasets (CodeReviewer, then ContextCRBench), reviewers (Qwen2.5-Coder-3B/7B frozen, CodeReviewer checkpoint, closed panel), candidate context types, perturbation implementation and validity audit, deterministic decoding, seeds, caching, leakage controls. Citation quota 5-8. Figure quota 0-1 (candidate context table).
5. **Results** — Five subsections mapped to claims: (5.1) baseline accuracy and flip rates (C2); (5.2) marginal value of context types, including per-example distribution (C3); (5.3) exhaustive minimal-context target and greedy approximation gap (C4); (5.4) stability-constrained vs correctness-only selection with controls and cost frontier (C1); (5.5) cross-reviewer transfer (C5). Citation quota 3-6. Figure quota 4-6.
6. **Discussion, Limitations, and Error Analysis** — Where selection fails and why; degenerate-stability checks; scope of generalization; honest treatment of any negative result. Citation quota 3-5. Figure quota 0-1.
7. **Conclusion** — Contribution and the single takeaway for practitioners. Citation quota 0-2. Figure quota 0.

## Totals

- Target citations: 35-45
- Target figures/tables: 7-9
- Target pages: 5-8 plus appendix

## Placeholder Discipline

Every number in Section 5 starts as an explicit placeholder and is resolved only by a logged run under `results/`. Claim language stays at hypothesis strength until the backing row in `brief/evidence-matrix.csv` is marked verified.

## Appendix Plan

Full prompt templates, perturbation examples with audit outcomes, per-model hyperparameters, complete subset-enumeration tables, cache and seed details, reproduction commands.
