# Innovation Candidates

Generated 2026-09-19 from `docs/ml2_project_proposal_detailed.docx` plus a verified literature scan. Each row is falsifiable and scoped to what this project can actually run.

## Candidate Table

| ID | Candidate | Gap vs prior work | Key references | Evidence plan | Risks / objections | Status |
|---|---|---|---|---|---|---|
| I1 | Measure instability of LLM code-review judgments under semantics-preserving changes | Thin. Mutation robustness is documented for output/execution prediction; extending it to review verdicts is incremental on its own | morvalho2026mutations, execsem2026 | Phase 1 flip-rate study | "Already known"; a descriptive result with no method | rejected as primary, retained as C2 premise |
| I2 | Rank which context types improve review correctness | Thin. ContextCRBench already reports aggregate context effects | hu2025contextcrbench | Phase 2 ablation | Duplicates an existing benchmark finding | rejected as primary, retained as C3 |
| I3 | **Stability-constrained minimal context selection**: select the smallest per-example subset meeting both a correctness and a perturbation-stability threshold | Real. Minimal-context objectives in prior work optimize task success only, on issue resolution and agent pruning, with no stability term and no review decision | msc2026issue, swepruner2026, hu2025contextcrbench, swe_prbench2026 | Exhaustive target on p=4-5, greedy approximation, controls at matched token budget | Greedy is not novel; needs the degenerate-stability control | **selected as primary (C1)** |
| I4 | Cross-reviewer transfer of selected context | Real but narrower. Prompt transfer is studied; transfer of a selected context subset for review decisions is not | promptbridge2025 | Phase 5 transfer matrix | Cost-limited; may yield a negative result | selected as supporting claim (C5) |
| I5 | White-box attribution-guided context ranking using logits/gradients/hidden states | Adjacent work exists via attention probing; would be a second method rather than a sharper question | sentinel2025 | Optional comparison against black-box selectors | Risk of noisy attributions consuming the timeline | deferred to optional extension |
| I6 | Reconcile the disagreement over whether more context helps code review | Real and cheap. ContextCRBench reports gains; SWE-PRBench reports monotone degradation | hu2025contextcrbench, swe_prbench2026 | Per-example gain distribution rather than aggregate means | Framing risk: is it a question or just an observation? | folded into C3 as motivation |

## Selection Notes

- Chosen primary contribution: **I3**, recorded as claim `C1` in `brief/contribution-map.yaml`.
- Why this is the right main claim: it is the only candidate that is simultaneously unclaimed by prior work, runnable on frozen 3B/7B models within ten weeks, falsifiable by a specific comparison (stability-constrained vs correctness-only at matched budget), and still informative if the result is negative.
- Rejected candidates and why: I1 and I2 restate findings already published in adjacent settings; they survive as premises, not contributions. I5 adds a second method without sharpening the question and is gated behind the black-box result. I6 lacks standalone weight and works better as motivation for C3.
