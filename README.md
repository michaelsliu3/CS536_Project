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
defaults to 3B QLoRA on NVIDIA CUDA, with notebook preparation on the M4 Max.

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

### Linux (NVIDIA GPU)

See [AGENTS.md](AGENTS.md) for full virtualenv setup (`torch==2.14.0+cu130`, notebook deps, kernel registration, verification).

### macOS (Apple Silicon)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
```

> Use Python 3.11 if available.

For the exploratory training notebook, select the **CS536 (.venv)** kernel and
set `HARDWARE = "auto"` (or explicitly `"nvidia"` / `"m4_max"`) in its configuration
cell. Both profiles default to 3B QLoRA, 2048-token sequences, and all rows
for one epoch. Prepare the notebook on the M4 Max, then train on an NVIDIA
CUDA cloud machine; the Mac training preflight stops before downloads. The notebook's one-time install cell selects packages for the host;
restart the kernel after installation. Mac preparation does not require bitsandbytes.
The data and model download cells still download multi-GB files when executed.

## LLaMA-Factory web UI

The optional [LLaMA-Factory](https://github.com/hiyouga/LLaMA-Factory/tree/v0.9.5)
UI uses a separate Python environment so its dependency versions do not change
the notebook's `.venv`. Install from the repository root with `uv`:

```bash
mkdir -p .cache/llamafactory
git clone --depth 1 --branch v0.9.5 https://github.com/hiyouga/LLaMA-Factory.git .cache/llamafactory/source
uv venv --python 3.11 .cache/llamafactory/venv
uv pip install --python .cache/llamafactory/venv/bin/python -e .cache/llamafactory/source
```

Launch it from the repository root:

```bash
./scripts/llamafactory_ui.sh
```

Open [the local UI](http://127.0.0.1:7860). Stop it with Ctrl+C in its terminal.
The launcher binds to localhost and disables public sharing. Set
`GRADIO_SERVER_PORT=7861` before the command if port 7860 is occupied.

For the exploratory Qwen run, select **Qwen2.5-Coder-3B-Instruct**, fine-tuning
method **lora**, quantization **4**, quantization method **bnb**, and template
**qwen**. Prepare settings on the Mac; execute QLoRA training on NVIDIA CUDA.
Installing the UI does not register ContextCRBench or convert the notebook's
data pipeline. Prepare repository-held-out training/evaluation files and register
them in LLaMA-Factory's `data/dataset_info.json` before training; its sample
instruction datasets are not ContextCRBench.

The source checkout, environment, UI state, and generated UI configurations live
under ignored `.cache/llamafactory/`. Copy configurations you want to keep into
`configs/`; put large adapters under ignored `checkpoints/`. The UI does not
inherit settings from the notebook automatically. On a cloud NVIDIA machine,
install CUDA PyTorch and bitsandbytes in the separate LLaMA-Factory environment
before training.

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

## Evaluate a completed Qwen3B adapter

Open `notebooks/qwen3b_evaluation_comparison.ipynb` with **CS536 (.venv)**.
Set the adapter directory (including tokenizer files and `run_manifest.json`),
extracted dataset path, and frozen split manifest. The run must have used that
same split; older checkpoints need their original split provenance recovered.
Set `RUN_INFERENCE=True` on cloud NVIDIA to compare the training-majority
classifier, untouched Qwen3B, and the trained adapter on identical validation
examples. Test evaluation is separately gated. The notebook performs no training.
Copy its results directory to the Mac and leave inference disabled to display
cached metrics, the comparison graph, and fixed demonstration examples.
