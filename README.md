# CS536 Project: Learned Context Selection for LLM Code Review

This repository studies whether a learned per-diff selector can preserve a
frozen LLM reviewer's ContextCRBench quality-assessment accuracy while reducing
optional context tokens. The primary reviewer is
`Qwen/Qwen2.5-Coder-7B-Instruct`; the 3B model supports lower-cost pilots.

Frozen inference supports:

- Windows + NVIDIA GPU (CUDA)
- Apple Silicon Mac (MPS)

The central experiment keeps Qwen frozen and trains a compact context selector.
`notebooks/qwen3b_qlora_contextcrbench.ipynb` is a separate exploratory QLoRA
feasibility test requested for the 3B reviewer; it is not the main method and
requires an NVIDIA CUDA GPU.

## Repository Layout

```
CS536_Project/
├── src/
│   ├── models/
│   │   ├── __init__.py
│   │   └── qwen.py
│   ├── evaluation/
│   │   └── __init__.py
│   ├── perturbations/
│   │   └── __init__.py
│   └── context_selection/
│       └── __init__.py
├── scripts/
│   ├── check_device.py
│   └── test_qwen.py
├── configs/
│   └── qwen7b.yaml
├── data/
├── results/
├── checkpoints/
├── notebooks/
├── requirements.txt
├── .gitignore
└── README.md
```

## Environment Setup

### Windows

```powershell
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install "torch==2.14.0+cu130" --index-url https://download.pytorch.org/whl/cu130
pip install -r requirements.txt
```

The explicit PyTorch index is required for NVIDIA use on Windows; the generic
PyPI wheel is CPU-only. Restart any active Jupyter kernel after replacing
PyTorch.

### macOS (Apple Silicon)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

> Use Python 3.11 if available.

## Verify Hardware/Runtime Detection

From project root:

```bash
python scripts/check_device.py
```

This prints Python/PyTorch versions, CUDA and MPS availability, and selected
device (`cuda`, `mps`, or `cpu`).

## Run Qwen Inference Smoke Test

```bash
python scripts/test_qwen.py
```

The script:

- loads `Qwen/Qwen2.5-Coder-7B-Instruct`
- uses chat-format messages
- runs deterministic generation (`temperature=0`, `do_sample=False`)
- reports model loading time and generation time

## Notes

- Hugging Face model weights are not committed to Git; each machine uses its
  own local HF cache.
- No quantization is enabled yet (`quantization: none`) to keep baseline
  behavior clean for later experiments.
- The model wrapper is structured to support future work on logits, hidden
  states, gradients, LoRA, and evaluation without changing high-level scripts.

## Troubleshooting

- If CUDA is expected but `torch.cuda.is_available()` is `False`, verify NVIDIA
  drivers and install a CUDA-enabled PyTorch wheel from the official PyTorch
  installation page.
- If MPS is expected but unavailable, verify Apple Silicon + recent macOS and a
  compatible PyTorch build.
