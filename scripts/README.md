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
| `setup/` | `download_reviewer_model.py` | Downloads the outcome table's reviewer, `Qwen/Qwen3.8-27B` at the revision `notebooks/outcome_table.ipynb` pins (about 55.6 GB), through hf-mirror.com into the Hugging Face cache. It never contacts huggingface.co, which is unreachable from mainland China (a request that would go there stops the run, and the log lists every host contacted); the mirror redirects the large weight files to Hugging Face's file CDN under hf.co, which is reachable there. It verifies every file, and prints the `vllm serve` command for the `cloud` profile. Re-run it to resume: finished files are kept and a partly downloaded weight file continues where it stopped. A connection that stays below 500 KB/s is replaced by a new one (`--min-speed`). `--dry-run` downloads nothing. |
| | `check_device.py` | Checks the PyTorch device (CUDA, MPS or CPU). |
| | `test_qwen.py` | Loads Qwen2.5-Coder-7B for a smoke test (large download). |
| | `llamafactory_ui.sh` | Starts the local LLaMA-Factory web UI (see the root README). |
| `outcome/` | `run_reviews.py` | Runs the outcome table's review stage from a terminal with the notebook's profile (`cloud` by default), so a long run can sit in `screen`. Saves each review as it arrives, resumes on re-run, and logs every call to the run folder's `logs/`. `--first N` reviews only the first N PRs and estimates the rest. |
| `analysis/` | `analyze_predictions.py` | Analyzes saved paired predictions (see `src/evaluation/README.md`). |
| | `preview_context_selection.py` | Previews one context policy with a local tokenizer, without inference. |
