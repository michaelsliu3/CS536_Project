# Scripts

Run every script from the project root with the project's environment, e.g.
`.venv/bin/python scripts/dataset/build_dataset.py data/aacr`. Each script finds the project
root from its own location, so the working directory only matters for relative arguments.

| Folder | Script | What it does |
|---|---|---|
| `dataset/` | `build_dataset.py` | Builds the context levels for every AACR-Bench PR into `ROOT/context_levels.jsonl` (setup checks, retries, logs, resume). See `src/tracer_context/README.md`. |
| | `clean_dataset.py` | Deletes a build and downloaded repositories so the next build starts over. |
| `tracer/` | `build_tracer_context.py` | Builds one review context for one PR checkout, with a config. |
| | `run_tracer_batch.py` | Builds per-PR folders (trace, every level's `context.md` and `selection.json`) for inspecting chosen AACR entries. |
| `setup/` | `check_device.py` | Checks the PyTorch device (CUDA, MPS or CPU). |
| | `test_qwen.py` | Loads Qwen2.5-Coder-7B for a smoke test (large download). |
| | `llamafactory_ui.sh` | Starts the local LLaMA-Factory web UI (see the root README). |
| `analysis/` | `analyze_predictions.py` | Analyzes saved paired predictions (see `src/evaluation/README.md`). |
| | `preview_context_selection.py` | Previews one context policy with a local tokenizer, without inference. |
