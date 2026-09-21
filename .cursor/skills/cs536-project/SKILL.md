---
name: cs536-project
description: Guides all CS536 / ML II research work in this repo: local Qwen inference, robustness, context selection, evaluation, writing, and repo changes. Use whenever working in CS536_Project, implementing models or scripts, planning experiments, editing docs, writing the proposal/report, or making research decisions.
---

# CS536 Project Skill

This is the Rutgers CS536 / Machine Learning II (Fall 2026) research project.

**Title:** Robust Minimal Context Selection for LLM Code Review

Before implementing, changing experiments, or writing course deliverables, read the relevant files in `docs/` and follow this skill. Do not rely on memory of a prior chat.

## Required first step

1. List `docs/` and identify current official sources (kickoff, proposal, later reports).
2. Read the source that matches the task. See [docs-index.md](docs-index.md).
3. If a request conflicts with `docs/` or this skill, say so and follow the official document unless the user explicitly overrides it.

## Research question

What is the smallest additional context needed to make an LLM code-review judgment correct and stable, and can that context transfer across reviewers?

Keep work tied to one of:

- **RQ1** Sensitivity of review judgments to semantics-preserving changes
- **RQ2** Which context types improve correctness and stability
- **RQ3** Minimal / near-minimal context subsets
- **RQ4** Transfer of selected context to other open and closed reviewers
- **RQ5** Token cost vs reliability

## Course standards (kickoff)

Reward depth, clarity, and evidence. Novelty is not required.

Do:

- State a question that can be answered in ten weeks
- Use baselines, ablations, and multiple seeds
- Explain failures and negative results
- Keep a runnable repo from day one

Do not:

- Report one accuracy number with no baseline
- Reproduce a paper that already has public code as the whole project
- Treat "biggest model" as ambition
- Upload confidential, private, or access-restricted data/code to external AI tools

## Method constraints (proposal)

- Reviewers stay **frozen**. This is an inference / analysis project, not pre-training.
- Do **not** full-fine-tune. LoRA / QLoRA only if the user asks after a baseline needs it.
- Main local reviewer: `Qwen/Qwen2.5-Coder-7B-Instruct`
- Fast iteration model (when added): `Qwen2.5-Coder-3B-Instruct`
- First dataset: Microsoft CodeReviewer quality estimation
- Later rich-context dataset: ContextCRBench
- Initial task: structured / binary review judgment, not free-form comments
- Deterministic decoding when measuring input-caused instability
- Cache model responses; fixed seeds
- Do not leak labels or human review comments into candidate context
- Choose thresholds on development data, not the test set
- Commercial APIs only after the selector is mostly fixed, on a small subset

## Repo conventions

- Work inside `CS536_Project/`. Do not create a nested project folder.
- Python 3.11 + `.venv`. Hugging Face Transformers, not Ollama/LM Studio.
- Put reusable code in `src/` (`models`, `evaluation`, `perturbations`, `context_selection`).
- Put one-off runners in `scripts/`. Configs in `configs/`.
- Small experiment outputs may go in `results/`. Large weights stay out of Git.
- Each machine uses its own Hugging Face cache. Do not commit `*.safetensors`, `*.bin`, `*.pt`, `*.pth`, `.venv/`, or `checkpoints/*` payloads.
- Keep `QwenCoder` the shared model interface. Preserve access to logits, hidden states, gradients, and later LoRA.
- Stay platform-neutral: CUDA on NVIDIA Windows, MPS on Apple Silicon, CPU fallback. No CUDA-only or Apple-only required packages.

## Current milestone

Confirm local inference first. Do not add CodeReviewer downloads, LoRA training, experiment trackers, or large datasets unless the user asks.

## Writing course documents

When drafting the proposal, midpoint check-in, or final report, follow the kickoff milestone checklist in [docs-index.md](docs-index.md). Acknowledge people, tools, prior code, and LLM assistance.
