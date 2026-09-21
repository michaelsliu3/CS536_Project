# Router Decision

Route to **empirical-paper-writer** (`../empirical-paper-writer/SKILL.md`).

## Chosen Mode

`empirical`

## Rationale

The primary claim C1 asserts that a stability-constrained selection objective produces more robust review decisions than a correctness-only objective at comparable token cost. That is a comparative statement about model behavior, and nothing in the literature can settle it; it needs runs, controls at matched budget, and seeds. Every claim C1 through C5 has at least one row in `brief/evidence-matrix.csv` whose `Evidence_Type` is `experiment`, and the citation rows exist only to position the work, not to support the main claims. The course rubric also demands baselines, ablations, and multiple seeds, which is an experimental contract rather than a synthesis one.

## Alternative Path

A `review` route would mean writing a survey of code-review robustness and context-selection methods, with the ContextCRBench versus SWE-PRBench disagreement as its organizing tension and a taxonomy of context types as the deliverable. For that to be the right call, C1 would have to be demoted to future work and the paper would have to be judged on coverage and organization instead of measurement. It was rejected because the project already owns local inference capability, the proposal commits to measurement, and a survey would discard the one contribution that prior work has not made.

## Adversarial Challenge

The strongest argument against routing empirical: the genuinely novel content here is conceptual, not experimental. Minimal sufficient context is already formalized, greedy forward and backward selection are textbook, and the perturbation families are borrowed. If the experiments land inside noise, which is plausible given a 16-to-32 subset space where the two objectives may rarely disagree, the paper has no result and would have been stronger as a synthesis that reconciles the conflicting context findings. A review paper is also far cheaper and carries no compute risk on a machine that currently runs CPU-only PyTorch.

Assessment of the challenge: it identifies a real risk but does not overturn the route. A null result on C1 is still an empirical finding, and the supporting claims stay informative even then, since flip rates on review judgments (C2), the per-example non-monotonicity of context value (C3), and the greedy-versus-exhaustive gap (C4) are measurements nobody has reported for this task. The challenge does force three commitments, now recorded in `brief/contribution-map.yaml` and `brief/evidence-matrix.csv`: report the objective-disagreement rate as a first-class result so power is visible, fix thresholds and the noise band before the test run, and measure held-out stability so the selector is not credited for fitting its own perturbations. The compute risk is a setup problem, not a routing problem.

## Final Verdict

Confirmed: `empirical`.

## Downstream Notes

- Template deviation: `empirical-paper-writer` ships IEEEtran assets, but the course requires a NeurIPS-style template for the 5-8 page final report. Resolve this at the writer's scaffold step instead of restyling later.
- Milestone reality: the course proposal (1 page, ~Sep 28) and midpoint check-in (≤ 3 pages, ~Nov 2) come before the full draft. Treat the full outline contract as the December target and derive the earlier documents from the same artifacts.
- Sequencing: the repo is still at the local-inference milestone, so the writer should generate placeholder-safe results and leave numbers unresolved until `results-backfill` runs.

## Collaboration Hook Status

- Hook 1 (Gemini `literature-map`): **not run.** Gemini CLI is not installed. Literature mapping was performed directly by the orchestrator; the nine anchor works and their findings are recorded in `brief/topic-brief.md`.
- Hook 2 (Claude `claim-stress-test`): **attempted and failed.** Claude CLI is installed but returned `Failed to authenticate: OAuth session expired`. The orchestrator ran the stress test instead; the resulting weaknesses were folded into `risk_factors` and `likely_reviewer_objections`, and C2, C5, and the C1 measurement protocol were tightened in response.
- Hook 3 (evidence sufficiency / gap check): **not run**, same authentication blocker. Evidence coverage is now 4-9 rows per claim, above the skill's threshold of 3.
- Hook 4 (Claude `route-review-vs-empirical`): **not run.** The adversarial challenge above was produced by the orchestrator to satisfy the mandatory challenge step.
- To enable these hooks later: run `claude` once and `/login`, then re-run the hooks from `.cursor/skills/collaborating-with-claude/`.

## Handoff Date

2026-09-19
