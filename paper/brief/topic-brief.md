# Topic Brief

## Topic
Stability-constrained minimal context selection for LLM code review: finding the smallest context subset that keeps a review judgment both correct and stable under semantics-preserving input changes, and testing whether that subset transfers to other reviewers.

## Scope

In scope:

- Structured review decisions: hunk-level quality estimation (does this change need a review comment?), later line-level defect localization.
- Frozen reviewers only. Inference and analysis, no training of the reviewer.
- Semantics-preserving perturbations: identifier renaming, formatting/whitespace, neutral comment edits, prompt paraphrase.
- Per-example context subset selection over a small candidate set (p = 4–5 context types).
- Exhaustive minimal-context optimum on a controlled subset, then greedy forward/backward approximation.
- Cross-reviewer transfer: second open model plus a cost-controlled closed-model subset.

Explicitly out of scope:

- Fine-tuning, LoRA, or QLoRA training of the reviewer.
- Free-form review comment generation as the primary evaluation target.
- Repository-scale retrieval infrastructure or agentic multi-turn review.
- Claims about human reviewer agreement or production deployment impact.
- Any formal Rashomon-set argument. Model multiplicity is motivation only.

## Audience
Researchers and practitioners in ML for software engineering, LLM robustness, and context/prompt efficiency. Immediate audience is the CS536 / ML II course staff.

## Constraints

- **Target**: CS536 / ML II final report, NeurIPS-style template, with an optional later arXiv/workshop version
- **Page target**: 5-8 pages plus unlimited appendix
- Deadline: final report ~Tue Dec 1 2026; talk Dec 3/7/10; midpoint check-in (≤ 3 pages) ~Mon Nov 2
- Compute: local consumer GPU / Apple Silicon, frozen Qwen2.5-Coder-3B and 7B; Colab and Rutgers iLab as overflow
- Budget: commercial API calls only on a small stratified subset, after the selector is fixed
- Special requirements: public GitHub repo, fixed seeds, runnable README, acknowledgement of tools and LLM assistance; no confidential or access-restricted data sent to external services
- Deviation to resolve downstream: the empirical writer skill ships IEEEtran assets, but the course requires a NeurIPS-style template

## Key Terms
LLM code review, code change quality estimation, semantics-preserving perturbation, prediction flip rate, robust correctness, minimal sufficient context, context selection, context pruning, token budget, cross-model transfer, model multiplicity, Qwen2.5-Coder, CodeReviewer, ContextCRBench

## Literature Map (verified via search, 2026-09-19)

| Citekey | Work | What it establishes | Why it matters here |
|---|---|---|---|
| `li2022codereviewer` | Li et al. 2022, CodeReviewer (arXiv 2203.09095) | Pre-trained code-review model; quality estimation / comment generation / refinement tasks and dataset | Source of our initial task, labels, and a task-specific baseline |
| `hu2025contextcrbench` | Hu et al. 2025, ContextCRBench (arXiv 2511.07017, FSE'26) | Context-rich line-level benchmark; textual context (issue/PR) helps quality estimation more than surrounding code; open-source models can degrade on localization when context is added | Defines our candidate context types; shows aggregate context value but not per-example minimality |
| `swe_prbench2026` | SWE-PRBench (2026) | Review quality degrades monotonically as context is enriched; a concise summary prompt beats a full-context prompt; attributed to attention dilution rather than content selection | Direct tension with ContextCRBench; motivates per-example selection instead of a global context recipe |
| `morvalho2026mutations` | Are LLMs Robust Against Semantics-Preserving Mutations? (arXiv 2505.10443; EPIA 2026) | Five semantics-preserving mutations; prediction changes and drops up to 70%; renaming strongly shifts behavior; Qwen2.5-Coder among affected models | Establishes that our perturbation premise is real, but on output prediction, not review judgment |
| `execsem2026` | How Robustly do LLMs Understand Execution Semantics? (arXiv 2604.16320) | Meaning-preserving transformations plus identifier renaming; semantic preservation sanity-checked by execution | Method template for validating that our perturbations really preserve the label |
| `msc2026issue` | Compressing Code Context for LLM-based Issue Resolution (arXiv 2603.28119) | Defines minimal sufficient context; 1-minimal subsequence found by genetic search plus hierarchical delta debugging; budget-aware greedy selection at inference | Closest prior art on minimality; optimizes task success only, no stability constraint, different task |
| `swepruner2026` | SWE-Pruner (arXiv 2601.16746) | Goal-conditioned line-level context pruning for coding agents; chunk-level pruning suits code better than token-level | Shows pruning is an active area; ours is decision-level and stability-aware, not agent middleware |
| `sentinel2025` | Sentinel (arXiv 2505.23277) | Attention-probing context compression; selects utilized sentences under a length budget | Reference point for our optional white-box attribution extension |
| `promptbridge2025` | PromptBridge (arXiv 2512.01420) | Prompts optimized for one model transfer poorly without adaptation | Supports RQ4 being a real open question rather than an assumed positive |

## Identified Gap

Three literatures stop short of our question:

1. Robustness work measures instability under semantics-preserving mutations, but on output/execution prediction, and offers no remedy.
2. Code-review context work measures aggregate gains for fixed context combinations, and prior results conflict on whether more context helps.
3. Minimal-context work optimizes correctness or patch success only, for issue resolution and agents, and never constrains stability or tests whether the selected subset transfers to a different reviewer.

No prior work defines the per-example minimal context subset for a code-review decision under a joint correctness-and-stability constraint, nor measures transfer of that subset across reviewers.
