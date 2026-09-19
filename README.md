# CS536 Project: Local Qwen2.5-Coder-7B Inference Setup

This repository provides a clean, reproducible setup for local inference with
`Qwen/Qwen2.5-Coder-7B-Instruct` using Python 3.11, PyTorch, and Hugging Face
Transformers. It is designed to run on:

- Windows + NVIDIA GPU (CUDA)
- Apple Silicon Mac (MPS)

The current milestone is inference only (no fine-tuning yet).

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
pip install -r requirements.txt
```

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
