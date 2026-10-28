# Agent instructions (CS536_Project)

## Required project context — all agents, every task

These instructions apply to every agent working anywhere in this repository, on
any platform, including setup, coding, debugging, notebooks, experiments, reviews,
and writing.

Before starting work:

1. Recursively list **all files in `docs/`**. Read **every document in full** for
   project context, including PDFs, DOCX files, Markdown, and documents added
   later. Do not select only documents that appear relevant to the immediate task.
2. Read `.agents/skills/cs536-project/SKILL.md` (Codex) or
   `.cursor/skills/cs536-project/SKILL.md` (Cursor), plus its `docs-index.md`.
   The index and skill summaries do not replace reading the original documents.
3. Use the documents to understand the project's purpose, professor feedback,
   research scope, decisions, constraints, and current status before acting.
   Distinguish historical notes and planned work from verified current results.
4. If a document cannot be read, identify the file and limitation explicitly;
   do not silently skip it or claim to have reviewed all context. Try available
   document extraction or rendering tools first.
5. Surface conflicting guidance. Follow explicit current user instructions;
   otherwise prefer current official course/instructor guidance and the latest
   applicable project decisions over older drafts or historical handoffs.
   Ask only when a material conflict remains unresolved.

At the start of each new task/session, re-list `docs/` and read any new or changed
documents. Documents already read in full in the same active session need not be
reread unless they change. Do not rely solely on memory from a previous chat.
These requirements also apply to delegated agents; include this context requirement
in any handoff.

## Linux NVIDIA environment setup

Instructions for agents setting up a **new Linux machine** with NVIDIA GPU. Windows setup is in [README.md](README.md).

Project skill and course docs: `.cursor/skills/cs536-project/SKILL.md` and `docs/`.

## Goal

Create `.venv` at the repo root with **CUDA 13.0 PyTorch** and dependencies for:

- `notebooks/qwen3b_qlora_contextcrbench.ipynb` (QLoRA / LoRA smoke tests)
- `scripts/check_device.py`, `scripts/test_qwen.py`

Do **not** commit `.venv/`, large weights, or `data/contextcrbench/` payloads. Do **not** run full training or download ContextCRBench unless the user asks.

## Prerequisites

From repo root, confirm:

```bash
python3 --version   # prefer 3.11; 3.12 is OK
nvidia-smi
git --version
```

QLoRA requires a working NVIDIA driver and enough disk for Hugging Face cache (multi‑GB per model).

## Create virtual environment

```bash
cd /path/to/CS536_Project
python3.11 -m venv .venv || python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
```

## Install PyTorch (CUDA 13.0) first

Generic PyPI `torch` is often **CPU-only**. Use the official wheel:

```bash
pip install "torch==2.14.0+cu130" --index-url https://download.pytorch.org/whl/cu130
```

## Install remaining dependencies

```bash
pip install -r requirements.txt
pip install "transformers>=4.56,<6" "accelerate>=1.5" "datasets>=3.4" \
  "peft>=0.15" "bitsandbytes>=0.45" "gdown>=5.2" "scikit-learn>=1.6" \
  "ipykernel>=6.29" "jupyter" "ipywidgets>=8.1" "tqdm"
```

If `requirements.txt` overwrote torch with a CPU build, reinstall CUDA torch:

```bash
pip install --force-reinstall "torch==2.14.0+cu130" --index-url https://download.pytorch.org/whl/cu130
```

## Jupyter kernel (Cursor / VS Code)

```bash
python -m ipykernel install --user --name cs536-project --display-name "CS536 (.venv)"
```

Select **CS536 (.venv)** or `.venv/bin/python` when running the notebook.

After changing torch or venv packages, **restart the notebook kernel** before training.

## Verification (required before reporting success)

```bash
python scripts/check_device.py
python -c "import torch; print(torch.__version__, torch.version.cuda, torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'no gpu')"
python -c "import transformers, peft, bitsandbytes, datasets; print('imports ok')"
```

Expected: `2.14.0+cu130`, CUDA `13.0`, `torch.cuda.is_available()` is `True`, GPU name printed.

## Remote SSH (Cursor)

Logs like `cursor-server-…` downloading over `ssh_tunnel` are **Cursor installing its remote server**, not project data. Wait for that to finish before relying on the remote kernel.

## Paths agents should know

| Item | Location |
|------|----------|
| HF model cache | `~/.cache/huggingface/hub` |
| ContextCRBench (after notebook download) | `data/contextcrbench/` |
| LoRA checkpoints | `checkpoints/qwen2.5-coder-{3b,7b}-contextcrbench-{qlora,lora}-smoke/` |
| Notebook run config | **Model and run configuration** cell (`MODEL_SIZE`, `USE_QLORA`, sample limits) |

## Optional (user must request)

- Run `python scripts/test_qwen.py` (loads 7B; long download).
- Execute notebook training cells or ContextCRBench download.

Report back: Python version, torch/CUDA check output, GPU name, and any install fixes applied.
