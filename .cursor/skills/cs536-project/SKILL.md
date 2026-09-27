---
name: cs536-project
description: Guides all CS536 / ML II research work in this repo: local Qwen inference, robustness, context selection, evaluation, writing, and repo changes. Use whenever working in CS536_Project, implementing models or scripts, planning experiments, editing docs, writing the proposal/report, or making research decisions.
---

# CS536 Project Skill

This is the Rutgers CS536 / Machine Learning II (Fall 2026) research project.

**Title:** Learning to Select Context per Code Diff: An Accuracy-Cost Trade-off for LLM Code Review

Before implementing, changing experiments, or writing course deliverables, read the relevant files in `docs/` and follow this skill. Do not rely on memory of a prior chat.

## Required first step

1. List `docs/` and identify current official sources (kickoff, proposal, later reports).
2. Read the source that matches the task. See [docs-index.md](docs-index.md).
3. If a request conflicts with `docs/` or this skill, say so and follow the official document unless the user explicitly overrides it.

## Research question

Can a small learned selector choose context separately for each code diff, maintaining a frozen LLM reviewer's hunk-level quality-assessment accuracy while using fewer input tokens than fixed context choices?

Keep work tied to one of:

- **RQ1** Effects of fixed issue, PR, and surrounding-code context on accuracy and token count
- **RQ2** Whether a fine-tuned compact selector improves the accuracy-cost frontier
- **RQ3** When context helps, does nothing, or turns a correct judgment into an incorrect one
- **Stretch** Transfer of selected subsets to a second frozen reviewer

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

- The central comparison keeps reviewers **frozen**; train the context selector, not Qwen.
- Exploratory Qwen LoRA / QLoRA is separate from the proposed main method and must be labeled as such.
- Main local reviewer: quantized `Qwen/Qwen2.5-Coder-7B-Instruct`
- Fallback/pilot reviewer: quantized `Qwen/Qwen2.5-Coder-3B-Instruct`
- Core dataset: ContextCRBench
- Core task: benchmark-defined hunk-level quality assessment, not free-form comments
- Candidate chunks: issue text, PR text, code before, and code after; diff is always present.
- Use deterministic decoding for the main reviewer comparisons.
- Cache model responses; fixed seeds
- Do not leak labels, merge status, human review comments, verdicts, or post-judgment text into model inputs.
- Split by repository where possible and keep all hunks from one PR in one partition.
- Choose thresholds on development data, not the test set
- Compare diff-only, all-context, best fixed, random, and similarity-based policies.

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

Audit and download ContextCRBench, verify its task mapping and chronology, then run a 100–200 example frozen-reviewer pilot. Do not scale outcome generation or selector training before the pilot passes its go/no-go checks.

## Writing course documents

When drafting the proposal, midpoint check-in, or final report, follow the kickoff milestone checklist in [docs-index.md](docs-index.md). Acknowledge people, tools, prior code, and LLM assistance.
