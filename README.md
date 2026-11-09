# CS536 Project: Learned Context Selection for LLM Code Review

This project studies whether a learned selector can decide how much repository
context a frozen LLM code reviewer needs, so that it keeps review quality while
sending fewer input tokens. The main experiment uses
[AACR-Bench](https://github.com/alibaba/aacr-bench): 196 pull requests from 50
repositories in ten languages, with 1,506 reference review comments.

The [roadmap](docs/context_selector_roadmap.md) records the design, every decision
with the options not chosen, and what has been measured. Its Section 2 has flow
diagrams of the architecture.

## Pipeline

1. **Dataset build** (no LLM). For every PR, a deterministic tracer,
   [codebase-memory-mcp](https://github.com/DeusData/codebase-memory-mcp) v0.12.1,
   builds ten nested context levels. They run from the PR text and diff
   (`L01_min`) to everything traced within three hops (`L10_all`). Each level
   shows everything its kinds and item limits allow, up to one cap of 256,000
   input tokens. The result is one file, `data/aacr/context_levels.jsonl`, with one
   line per PR.
2. **Outcome table.** The frozen reviewer, `Qwen/Qwen3.8-27B` with OpenAI Codex's
   `/review` rubric and greedy decoding, reviews every (PR, level) once. AACR's
   file-level semantic judge scores the findings against the reference comments.
   A level identical to its parent is reviewed only once.
3. **Selector.** Classical models (gradient-boosted trees, linear models, random
   forests and a small MLP) read input-side PR features and pick one level per PR.
   They are evaluated by looking up the outcome table. Item-level selection, which
   picks individual traced items, comes next.

## Status (November 8, 2026)

- **Built and tested:** the tracer, the ten levels, the dataset builder and the
  outcome-table code. The suite (`.venv/bin/python -m unittest discover -s tests`)
  runs 114 tests, all passing.
- **Run:** the dataset build on two FreeCAD PRs, committed as a sample in
  [data/aacr_sample](data/aacr_sample/README.md). The outcome table has only been
  smoke-tested, with a 0.8B model on two levels each of two PRs, which says nothing
  about review quality.
- **Not done:** the full 196-PR build, any Qwen3.8-27B review, the choice of judge
  model, and the selector.

## Quick Start

Set up the environment first (below). The dataset build also needs `git` and the
GitHub CLI `gh`, logged in. If they are missing, it downloads and verifies the
pinned AACR-Bench file and codebase-memory-mcp build, and downloads the reviewer's
tokenizer files.

Run the setup checks and see the build plan without building anything:

```bash
data/build_dataset.sh --dry-run
```

Build the dataset into `data/aacr/context_levels.jsonl`. Re-running resumes: it
skips PRs already built with the same inputs and retries failed ones.

```bash
data/build_dataset.sh
```

List the build and downloaded repositories, then delete them after asking, to
start over:

```bash
data/clean_dataset.sh
```

`DATASET_ROOT=<folder>` changes the dataset folder, and extra arguments go to
`scripts/dataset/build_dataset.py` or `clean_dataset.py` (see their `--help`). To
read the format without building, open the committed 2-PR sample,
`data/aacr_sample/context_levels.json`.

The outcome table is built by `notebooks/outcome_table.ipynb`. Choose its profile
with the `OUTCOME_PROFILE` environment variable:

- **`cloud`** (the default) calls Qwen3.8-27B through an OpenAI-compatible server,
  such as vLLM on the cloud GPU. Its judge is a placeholder that stops the notebook
  until the judge model is chosen.
- **`local-smoke`** runs `Qwen/Qwen3.5-0.8B` in-process on two levels of
  cline/cline#5177, with a keyword stand-in for the judge. It tests the pipeline
  only.

Results go to `data/aacr/outcomes/<run_name>/`: `table.csv` per (PR, level),
`coverage.csv` per reference, each cell's review and judgment, and `manifest.json`.
More detail: [src/tracer_context](src/tracer_context/README.md),
[src/review_outcomes](src/review_outcomes/README.md) and
[scripts](scripts/README.md).

## Repository Layout

```
CS536_Project/
├── src/
│   ├── tracer_context/       # tracer, context levels, dataset builder
│   ├── review_outcomes/      # reviewer runs, AACR judge, outcome table
│   ├── context_selection/    # midpoint: ContextCRBench context baselines
│   ├── evaluation/           # midpoint: splits, comparison, analysis, plots
│   ├── training/             # midpoint: training runtime and resume
│   ├── models/               # midpoint: Qwen inference wrapper
│   └── perturbations/        # empty placeholder
├── scripts/                  # see scripts/README.md
│   ├── dataset/              # build_dataset.py, clean_dataset.py
│   ├── tracer/               # build_tracer_context.py, run_tracer_batch.py
│   ├── setup/                # check_device.py, test_qwen.py, llamafactory_ui.sh
│   └── analysis/             # analyze_predictions.py, preview_context_selection.py
├── configs/
│   ├── levels/               # L01_min.yaml ... L10_all.yaml
│   ├── tracer_levels_base.yaml
│   ├── tracer_context.yaml   # standalone single-level tracer config
│   ├── review_prompts/       # pinned Codex /review rubric, with licenses
│   └── qwen7b.yaml           # midpoint inference config
├── data/
│   ├── build_dataset.sh      # build data/aacr/context_levels.jsonl
│   ├── clean_dataset.sh      # delete a build to start over
│   └── aacr_sample/          # committed 2-PR sample of the dataset
├── notebooks/
│   ├── outcome_table.ipynb
│   ├── qwen3b_qlora_contextcrbench.ipynb    # midpoint
│   └── qwen3b_evaluation_comparison.ipynb   # midpoint
├── tests/                    # unittest suite
├── docs/                     # roadmap, proposal, course documents, report template
├── paper/                    # midpoint report and plans
├── results/  checkpoints/  models/          # local outputs, not committed
├── log_training.sh           # midpoint: follow training logs
├── requirements.txt
├── AGENTS.md
└── README.md
```

Everything else under `data/` (the built dataset, clones, logs and outcome tables)
is git-ignored.

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

The dataset build runs on a laptop. Qwen3.8-27B does not fit the development
laptop (Apple M4 Max, 36 GB), so real outcome-table runs need the cloud GPU.

## Verify Hardware/Runtime Detection

From project root:

```bash
python scripts/setup/check_device.py
```

This prints Python/PyTorch versions, CUDA and MPS availability, and selected
device (`cuda`, `mps`, or `cpu`).

## Troubleshooting

- If CUDA is expected but `torch.cuda.is_available()` is `False`, verify NVIDIA
  drivers and install a CUDA-enabled PyTorch wheel from the official PyTorch
  installation page.
- If MPS is expected but unavailable, verify Apple Silicon + recent macOS and a
  compatible PyTorch build.

## Midpoint Pilot (ContextCRBench, historical)

Before the AACR-Bench work, the midpoint pilot fine-tuned
`Qwen/Qwen2.5-Coder-3B-Instruct` with QLoRA to predict ContextCRBench merge
status. It is kept as infrastructure and lessons, not as evidence for the
selector (roadmap Section 3), and its code and notebooks still run. The
[midpoint report](paper/midpoint/main.pdf) describes it.

### Training notebook

`notebooks/qwen3b_qlora_contextcrbench.ipynb` is the exploratory QLoRA
feasibility test for the 3B reviewer. It defaults to 3B QLoRA on NVIDIA CUDA,
with notebook preparation on the M4 Max.

Select the **CS536 (.venv)** kernel and set `HARDWARE = "auto"` (or explicitly
`"nvidia"` / `"m4_max"`) in its configuration cell. Both profiles default to 3B
QLoRA, 2048-token sequences, and all rows for one epoch. Prepare the notebook on
the M4 Max, then train on an NVIDIA CUDA cloud machine; the Mac training
preflight stops before downloads. The notebook's one-time install cell selects
packages for the host; restart the kernel after installation. Mac preparation
does not require bitsandbytes. The data and model download cells still download
multi-GB files when executed.

### Context-selection baselines

Context-selection baselines for ContextCRBench (`diff_only`, `all_context`,
`fixed`, `random`, and BM25 `similarity`) and a local tokenizer preview are
available in [src/context_selection](src/context_selection/README.md). They
enforce measured chat-token budgets without model inference; see that guide for
usage, tests, and evaluation limits.

### LLaMA-Factory web UI

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
./scripts/setup/llamafactory_ui.sh
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

### Run Qwen inference smoke test

```bash
python scripts/setup/test_qwen.py
```

The script:

- loads `Qwen/Qwen2.5-Coder-7B-Instruct`, the midpoint's primary model
  (`configs/qwen7b.yaml`)
- uses chat-format messages
- runs deterministic generation (`temperature=0`, `do_sample=False`)
- reports model loading time and generation time

Notes:

- Hugging Face model weights are not committed to Git; each machine uses its
  own local HF cache.
- No quantization is enabled (`quantization: none`) to keep baseline behavior
  clean.
- The model wrapper is structured to support logits, hidden states, gradients,
  LoRA, and evaluation without changing high-level scripts.

### Evaluate a completed Qwen3B adapter

Open `notebooks/qwen3b_evaluation_comparison.ipynb` with **CS536 (.venv)**.
Set the adapter directory (including tokenizer files and `run_manifest.json`),
extracted dataset path, and frozen split manifest. The run must have used that
same split; older checkpoints need their original split provenance recovered.
Set `RUN_INFERENCE=True` on cloud NVIDIA to compare the training-majority
classifier, untouched Qwen3B, and the trained adapter on identical validation
examples. Test evaluation is separately gated. The notebook performs no training.
Copy its results directory to the Mac and leave inference disabled to display
cached metrics, the comparison graph, and fixed demonstration examples.

For paired error analysis, repository-bootstrap intervals, CSV tables, and
report figures from those saved predictions, see the
[offline analysis guide](src/evaluation/README.md). For example:

```bash
python scripts/analysis/analyze_predictions.py \
  --predictions results/qwen3b-comparison/predictions.jsonl \
  --models majority base adapter --reference base \
  --output results/qwen3b-analysis-v1 --plots
```

Use the comparison notebook's actual output path. This command performs no
inference or training; `--plots` uses Matplotlib. Real prediction artifacts are
required before reporting model scores or token savings.

#### Cloud checkpoints and recovery

The training notebook fixes **3B QLoRA** on every profile; execution requires NVIDIA
CUDA. Set `CHECKPOINT_ROOT` to persistent cloud storage and keep the same `RUN_NAME`
to resume. Checkpoints save every 10 optimizer steps or about five minutes at the
next optimizer boundary, retaining three verified complete saves. Resume restores
adapter, optimizer, scheduler, RNG and Trainer progress. Logs append across sessions.
A completed run skips training when its epoch target is already met. Set
`NUM_TRAIN_EPOCHS=3` after one completed epoch to train three more epochs;
`TOTAL_NUM_EPOCHS` is computed as 4. An unfinished request keeps its saved target
on restart. After completion, rerunning the model/setup cell starts another request;
rerunning only the training cell keeps the existing target.

The notebook saves `training_config.json` in the run folder and each full checkpoint.
Matching training settings reuse the run; changed settings select a new suffixed
folder automatically. `RUN_NAME` names this family of experiments. Epoch targets,
logging cadence, package versions and code hashes do not force a new run; environment
and code provenance are still recorded. Keep compatible packages for loading saved
optimizer state. `./log_training.sh` follows the selected folder on launch.

Browser disconnects can leave the kernel running:
reconnect before launching another writer. Persistent storage or a backup is required
to survive instance/volume deletion; only work through the last intact checkpoint is
recoverable. See the midpoint plan for the full artifact and metric inventory.

## Related Papers

Every paper the project documents cite, grouped by how closely it matches what we
build. [Roadmap Section 15](docs/context_selector_roadmap.md#15-reading-and-citation-map)
has how each one fits and where it differs.

**Used directly**

- [AACR-Bench](https://arxiv.org/html/2601.19494v3)
  ([code](https://github.com/alibaba/aacr-bench)): the dataset and the semantic
  judge that scores the outcome table.
- [codebase-memory-mcp](https://arxiv.org/abs/2603.27277), a preprint
  ([code](https://github.com/DeusData/codebase-memory-mcp)): the tracer that
  builds the ten nested context levels.

**Closest methods**

- [Repoformer](https://proceedings.mlr.press/v235/wu24a.html) (ICML 2024): learns
  when extra retrieved code is worth adding (code completion).
- [RLCoder](https://arxiv.org/abs/2407.19487): learns retrieval from the downstream
  model's results; our outcome table gives that signal for reviews.
- [RECOMP](https://arxiv.org/abs/2310.04408) (ICLR 2024): selects only the useful
  retrieved passages, close to our planned item-level selection.
- [RepoHyper](https://arxiv.org/html/2403.06095v3)
  ([code](https://github.com/FSoft-AI4Code/RepoHyper)): code-graph retrieval; the
  first tracer choice, replaced because it is Python-oriented.

**Alternatives and baselines**

- [DraCo](https://arxiv.org/abs/2405.19782): dependency-aware retrieval.
- [GraphCoder](https://arxiv.org/abs/2406.07003): structure-aware coarse-to-fine
  retrieval.
- [RepoCoder](https://arxiv.org/abs/2303.12570): iterative retrieval baseline.

**Task and motivation**

- [ContextCRBench](https://arxiv.org/abs/2511.07017) (FSE Industry 2026): code
  review with fixed context choices; the midpoint pilot's dataset.
- [Shi et al.](https://proceedings.mlr.press/v202/shi23a) (ICML 2023): irrelevant
  context lowers accuracy (math reasoning, not code review).

**Background**

- [QLoRA](https://arxiv.org/abs/2305.14314): the midpoint's fine-tuning pilot.
- [PyCG](https://arxiv.org/abs/2103.00587) and
  [code property graphs](https://conferences.computer.org/sp/pdfs/sp/2014/4686a590.pdf):
  earlier graph-based program analysis.
- [ElliCE](https://papers.nips.cc/paper_files/paper/2025/hash/b34efa1182a3f96b2bc4cd844238ee31-Abstract-Conference.html)
  (NeurIPS 2025): conceptual inspiration only.

**Considered and superseded:** [CodeReviewer](https://arxiv.org/abs/2203.09095),
[LAURA](https://arxiv.org/abs/2512.01356) and
[SWE-PRBench](https://arxiv.org/abs/2603.26130).
