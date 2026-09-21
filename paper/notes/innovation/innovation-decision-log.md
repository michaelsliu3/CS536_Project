# Innovation Decision Log

## Log

- 2026-09-19: Ran paper-from-zero on `docs/ml2_project_proposal_detailed.docx`. Course constraints read from `docs/MLII_fall2026_project_kickoff.pdf`.
- 2026-09-19: Literature scan (web) produced nine anchor works. Gemini breadth hook unavailable (CLI not installed), so literature mapping was done directly.
- 2026-09-19: Created candidates I1-I6. Rejected I1 and I2 as primary claims because mutation robustness and aggregate context value are already published in adjacent settings.
- 2026-09-19: Selected I3 (stability-constrained minimal context selection) as the primary contribution C1. Rationale: unclaimed objective, feasible with frozen models, falsifiable at matched token budget.
- 2026-09-19: Discovered a direct novelty threat in `msc2026issue`, which already defines minimal sufficient context with a 1-minimal guarantee for issue resolution. Narrowed C1 to depend on the stability constraint and the review-decision setting rather than on minimality itself.
- 2026-09-19: Discovered a conflict between `hu2025contextcrbench` (context helps) and `swe_prbench2026` (context degrades review quality monotonically). Promoted this tension into the motivation for C3.
- 2026-09-19: Added the degenerate-stability control (constant predictor, random subset at matched budget) after noting that stability alone can be gamed.
- 2026-09-19: Claude `claim-stress-test` hook attempted; bridge returned `Failed to authenticate: OAuth session expired`. Fell back to an orchestrator-run adversarial review.
- 2026-09-19: Stress test produced three structural fixes: report the objective-disagreement rate (power), pre-register tau_c/tau_s and the noise band (circularity), and evaluate stability on held-out perturbation families (selection leakage). Sharpened C2 and C5 so neither can absorb every outcome.
- 2026-09-19: Routed to `empirical-paper-writer`. Flagged the IEEEtran vs NeurIPS template deviation for the writer stage.
