#!/usr/bin/env python3
"""Run the selector notebooks of the AACR 50% set on every reviewed pull request.

notebooks/selector50_nonllm.ipynb and notebooks/selector50_llm_a100.ipynb were written for the
first 93 pull requests and check that set's numbers. This script builds their three input files
for the full run and executes their code cells as they are, with the two checks of the 93's
numbers pointed at the new set's own counts. The notebooks themselves are not changed.

    python scripts/analysis/run_selector185.py inputs   # data/aacr_hunk_selector_files/
    python scripts/analysis/run_selector185.py nonllm   # the 8 non-LLM selectors (a laptop, minutes)
    python scripts/analysis/run_selector185.py llm      # the 13 LLM selectors (a GPU; 70 GB for all of them)
    python scripts/analysis/run_selector185.py llm --zero-shot-sizes 1.5 3 7 --skip-embeddings --skip-lora
                                                        # the zero-shot question to models up to 7B (an Apple laptop)

`inputs` writes hunk_outcomes.json (one row per reviewed hunk, and each reference's labels),
coverage.csv, context_levels_slim.jsonl and manifest.json. `--only-prs-in RUN` keeps the pull
requests of another run folder only, for example the frozen first run, to tell what changes with
the pull requests from what changes with the input facts.

One thing differs from the 50% run. The slim contexts hold the four reviewed levels only. The
pull requests added on November 27 were built with those four, so the sizes of the seven other
levels, which the 50% run's tabular facts included, do not exist for them.

`llm` reads results/selector185_nonllm/nonllm_oof_scores.csv when it is there, so its tables hold
the non-LLM models too. Every model output is cached per model, seed and fold under the results
folder, so a stopped run continues where it stopped. `--skip-lora` leaves out the fine-tuning,
which is most of the GPU time. `--zero-shot-sizes` and `--embedding-sizes` name the Qwen2.5-Coder
sizes to use in place of the notebook's lists (which go up to 14B).
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

LEVELS = ["H01_min", "H02_function", "H03_file", "H04_callees1"]
RUN = PROJECT_ROOT / "data/aacr_hunk/outcomes/qwen3.8-27b_aacr-hunk_kept4"
CONTEXTS = PROJECT_ROOT / "data/aacr_hunk/context_levels.jsonl"
LABELS = PROJECT_ROOT / "data/aacr_hunk/labels/severity_qwen3.8-27b_think.jsonl"
DATA = PROJECT_ROOT / "data/aacr_hunk_selector_files"
NOTEBOOKS = {"nonllm": "selector50_nonllm.ipynb", "llm": "selector50_llm_a100.ipynb"}
OUT = {"nonllm": PROJECT_ROOT / "results/selector185_nonllm", "llm": PROJECT_ROOT / "results/selector185_llm"}
# Parts of a PR's or a level's record that no notebook reads; left out of the slim contexts.
PR_DROP = ("audit", "build", "fetch_log", "hunk_checks", "known_paths")
LEVEL_DROP = ("included", "sections")

# The lines of the notebooks that hold the first 93's numbers, and what runs in their place.
REPLACEMENTS = [
    ("Y.sum() == 190, Y.sum()", "Y.sum() == EXPECT_POSITIVES, (Y.sum(), EXPECT_POSITIVES)"),
    ('== [342, 363, 357, 356], FIXED["covered"].tolist()', '== EXPECT_FIXED, (FIXED["covered"].tolist(), EXPECT_FIXED)'),
    ('print("Fixed levels match docs/aacr_50pct_run.md, Section 2.")', 'print("Fixed levels match coverage.csv:", EXPECT_FIXED)'),
    ('"note": "AACR-Bench 50% (93 PRs, 928 hunks, four levels). One reviewer run. No locked test set.",', '"note": RUN_NOTE,'),
]


def model_names(sizes: list[str]) -> list[str]:
    """['1.5', '7B'] -> the Qwen2.5-Coder Instruct models of those sizes, as the notebook names them."""
    return [f"Qwen/Qwen2.5-Coder-{size.upper().removesuffix('B')}B-Instruct" for size in sizes]


def llm_settings(args) -> dict:
    """What the `llm` options set after the notebook's configuration cell."""
    settings = {name: False for name, skip in (("RUN_ZERO_SHOT", args.skip_zero_shot), ("RUN_EMBED", args.skip_embeddings),
                                               ("RUN_FINETUNE", args.skip_lora)) if skip}
    for name, sizes in (("ZS_MODELS", args.zero_shot_sizes), ("EMB_MODELS", args.embedding_sizes)):
        if sizes:
            settings[name] = model_names(sizes)
    if args.zero_shot_sizes or args.embedding_sizes:              # draw the models of this run in the figure
        shown = ["tabular, logistic"]
        if args.zero_shot_sizes and not args.skip_zero_shot:
            shown += [f"zero-shot {name.split('-Coder-')[1].split('-')[0]}" for name in model_names(args.zero_shot_sizes)]
        if args.embedding_sizes and not args.skip_embeddings:
            shown += [f"embedding {name.split('-Coder-')[1].split('-')[0]}" for name in model_names(args.embedding_sizes)]
        settings["PLOT_METHODS"] = shown
    for name, value in (("ZS_BATCH", args.zero_shot_batch), ("EMB_BATCH", args.embedding_batch)):
        if value:
            settings[name] = value
    return settings


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


# ---------- inputs ----------

def slim_hunk(hunk: dict) -> dict:
    """A hunk with the reviewed levels only, and no context text except the first level's."""
    levels = {level: {key: value for key, value in hunk["levels"][level].items()
                      if key not in LEVEL_DROP and (key != "text" or level == LEVELS[0])} for level in LEVELS}
    return {**hunk, "levels": levels}


def expected_counts(run: Path, hunks: set[str], keep) -> dict:
    """What the notebooks' checks must find, counted from the run's own tables and not from the
    rows: the references each level covers, and the hunks where some wider level covers more."""
    covered = Counter(row["level"] for row in read_csv(run / "coverage.csv") if keep(row["entry"]) and row["covered"] == "True")
    per_hunk: dict[str, dict] = {}
    for row in read_csv(run / "hunk_table.csv"):
        if row["entry"] in hunks:
            per_hunk.setdefault(row["entry"], {})[row["level"]] = int(row["references_covered"])
    positives = sum(max(cells[level] for level in LEVELS[1:]) > cells[LEVELS[0]] for cells in per_hunk.values())
    return {"fixed_covered": [covered[level] for level in LEVELS], "positives": positives}


def build_inputs(run: Path, contexts: Path, labels: Path, out: Path, keep=lambda entry: True) -> dict:
    """Write the notebooks' input files for the PRs `keep` accepts; returns the manifest."""
    import selector_checks as checks
    from src.review_outcomes import severity

    table = checks.build_rows(contexts, run, LEVELS)
    tiers = {key: record.get("tier") for key, record in severity.load_labels(labels).items()}
    rows = [{key: value for key, value in row.items() if key != "pr_refs"} for row in table["rows"] if keep(row["pr"])]
    refs = {pr: {index: {**ref, "tier": tiers.get((pr, int(index)))} for index, ref in items.items()}
            for pr, items in table["refs"].items() if keep(pr)}
    out.mkdir(parents=True, exist_ok=True)
    (out / "hunk_outcomes.json").write_text(json.dumps({"rows": rows, "refs": refs, "levels": LEVELS}), encoding="utf-8")

    with (run / "coverage.csv").open(newline="", encoding="utf-8") as source, \
            (out / "coverage.csv").open("w", newline="", encoding="utf-8") as target:
        reader = csv.DictReader(source)
        writer = csv.DictWriter(target, reader.fieldnames)
        writer.writeheader()
        writer.writerows(row for row in reader if keep(row["entry"]))

    wanted = {row["hunk"] for row in rows}
    with contexts.open(encoding="utf-8") as source, (out / "context_levels_slim.jsonl").open("w", encoding="utf-8") as target:
        for line in source:
            entry = json.loads(line)
            if entry.get("status") != "ok" or not keep(entry["id"]):
                continue
            entry = {key: value for key, value in entry.items() if key not in PR_DROP}
            entry["hunks"] = [slim_hunk(hunk) for hunk in entry["hunks"] if hunk["id"] in wanted]
            target.write(json.dumps(entry) + "\n")

    references = [ref for items in refs.values() for ref in items.values()]
    manifest = {
        "run": str(run.relative_to(PROJECT_ROOT)), "contexts": str(contexts.relative_to(PROJECT_ROOT)),
        "labels": str(labels.relative_to(PROJECT_ROOT)), "levels": LEVELS,
        "prs": len({row["pr"] for row in rows}), "repositories": len({row["pr"].rsplit("__", 1)[0] for row in rows}),
        "hunks": len(rows), "references": len(references), "tiers": dict(Counter(ref["tier"] or "none" for ref in references)),
        "left_out": [hunk for hunk in table["left_out"] if keep(hunk.rsplit("__h", 1)[0])],
        "expected": expected_counts(run, wanted, keep),
        "sha256": {name: sha256(out / name) for name in ("hunk_outcomes.json", "coverage.csv", "context_levels_slim.jsonl")},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return manifest


# ---------- running a notebook ----------

def patched(source: str, used: Counter) -> str:
    for old, new in REPLACEMENTS:
        if old in source:
            used[old] += source.count(old)
            source = source.replace(old, new)
    return source


def run_notebook(kind: str, data: Path, out: Path, settings: dict | None = None) -> dict:
    """Execute the code cells of a selector notebook on the inputs in `data`, writing to `out`.
    `settings` are set after the configuration cell, for example {"RUN_FINETUNE": False}."""
    manifest = json.loads((data / "manifest.json").read_text(encoding="utf-8"))
    os.chdir(PROJECT_ROOT)
    os.environ["SELECTOR_DATA"], os.environ["SELECTOR_OUT"] = str(data), str(out)
    import matplotlib
    matplotlib.use("Agg")

    notebook = json.loads((PROJECT_ROOT / "notebooks" / NOTEBOOKS[kind]).read_text(encoding="utf-8"))
    cells = [(i, "".join(cell["source"])) for i, cell in enumerate(notebook["cells"]) if cell["cell_type"] == "code"]
    names = {"__name__": "__main__", "EXPECT_POSITIVES": manifest["expected"]["positives"],
             "EXPECT_FIXED": manifest["expected"]["fixed_covered"],
             "RUN_NOTE": f"AACR-Bench, {manifest['prs']} PRs, {manifest['hunks']} hunks, four levels; tabular facts from the "
                         "four reviewed levels only. One reviewer run. No locked test set."}
    used: Counter = Counter()
    for position, (index, source) in enumerate(cells):
        if position == 0:
            source = source.replace("raise RuntimeError(", "print(")   # the kernel check is for the notebook
        print(f"\n######## {NOTEBOOKS[kind]}, cell {index}", flush=True)
        exec(compile(patched(source, used), f"{NOTEBOOKS[kind]}[{index}]", "exec"), names)
        if position == 0 and "REFERENCE_METHOD" not in names:
            for name in ("FILES", "OUT_DIR"):
                print(f"  {name}: {names[name]}")
        if "TARGET_SPEND" in names and settings:      # the configuration cell has run
            names.update(settings)
            print(f"  set after the configuration cell: {settings}")
            settings = None
    wrong = {old: used[old] for old, _ in REPLACEMENTS if used[old] != 1}
    if wrong:
        raise SystemExit(f"the notebook changed: these lines were expected once each, found {wrong}")
    return names


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("inputs", help="build the notebooks' input files from a run")
    p.add_argument("--run", type=Path, default=RUN)
    p.add_argument("--contexts", type=Path, default=CONTEXTS)
    p.add_argument("--labels", type=Path, default=LABELS)
    p.add_argument("--only-prs-in", type=Path, help="a run folder; keep only the pull requests in its coverage.csv")
    p.add_argument("--out", type=Path, default=DATA)
    for kind in ("nonllm", "llm"):
        p = sub.add_parser(kind, help=f"execute notebooks/{NOTEBOOKS[kind]} on the input files")
        p.add_argument("--data", type=Path, default=DATA)
        p.add_argument("--out", type=Path, default=OUT[kind])
        if kind == "llm":
            p.add_argument("--nonllm", type=Path, default=OUT["nonllm"] / "nonllm_oof_scores.csv",
                           help="the non-LLM run's out-of-fold scores, put into the same tables when the file exists")
            p.add_argument("--skip-zero-shot", action="store_true")
            p.add_argument("--skip-embeddings", action="store_true")
            p.add_argument("--skip-lora", action="store_true")
            p.add_argument("--zero-shot-sizes", nargs="+", metavar="SIZE", help="model sizes for the zero-shot question, e.g. 1.5 3 7")
            p.add_argument("--embedding-sizes", nargs="+", metavar="SIZE", help="model sizes for the embeddings, e.g. 1.5 3 7")
            p.add_argument("--zero-shot-batch", type=int, help="hunks per batch for the zero-shot question (the notebook's is 4)")
            p.add_argument("--embedding-batch", type=int, help="hunks per batch for the embeddings (the notebook's is 8)")
    args = parser.parse_args(argv)

    if args.command == "inputs":
        keep = lambda entry: True
        if args.only_prs_in:
            only = {row["entry"] for row in read_csv(args.only_prs_in / "coverage.csv")}
            keep = lambda entry: entry in only
        manifest = build_inputs(args.run.resolve(), args.contexts.resolve(), args.labels.resolve(), args.out, keep)
        print(json.dumps({key: value for key, value in manifest.items() if key != "sha256"}, indent=1))
        print(f"wrote {args.out}")
        return 0

    settings = {}
    if args.command == "llm":
        args.out.mkdir(parents=True, exist_ok=True)
        if args.nonllm.is_file():
            shutil.copyfile(args.nonllm, args.out / "nonllm_oof_scores.csv")
        settings = llm_settings(args)
    run_notebook(args.command, args.data.resolve(), args.out.resolve(), settings)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
