"""Re-score every cached LLM selector of the AACR 50% run, next to the non-LLM models.

The LLM notebook (notebooks/selector50_llm_a100.ipynb) fits its model lists to the GPU it is
given. Its last Colab run got a T4, so its tables leave out the 7B models and the 3B LoRA,
whose outputs earlier sessions had already cached on Drive. That run also had no copy of the
non-LLM notebook's out-of-fold scores. This script executes the LLM notebook's cells with the
model lists set to what the cache holds, reads every model output from the cache (no model
is run; only the tokenizers are loaded, to rebuild the texts and their hash), and merges the
non-LLM scores, so all methods share one set of tables.

LoRA 3B is used with the seeds whose five folds are all cached (two of three in the run of
November 23, 2026), and is named "LoRA 3B (<n> seeds)".

Usage, from the project root, in the cs536 environment:
    python scripts/analysis/rescore_selector50_cache.py
Options: --cache (default results/selector50_llm_results/cache), --nonllm (default
results/selector50_nonllm/nonllm_oof_scores.csv), --out (default results/selector50_combined).
See docs/selector50_run.md.
"""
import argparse
import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

OVERRIDE = '''
ZS_MODELS = ["Qwen/Qwen2.5-Coder-1.5B-Instruct", "Qwen/Qwen2.5-Coder-7B-Instruct"]
EMB_MODELS = ["Qwen/Qwen2.5-Coder-0.5B-Instruct", "Qwen/Qwen2.5-Coder-1.5B-Instruct",
              "Qwen/Qwen2.5-Coder-3B-Instruct", "Qwen/Qwen2.5-Coder-7B-Instruct"]
FT_MODELS = ["Qwen/Qwen2.5-Coder-0.5B-Instruct"]
PLOT_METHODS = ["tabular, logistic", "zero-shot 1.5B", "zero-shot 7B", "embedding 3B", "embedding 7B",
                "embedding + tabular 7B", "LoRA 0.5B", "LoRA 3B (2 seeds)",
                "non-LLM: tabular+text, boosted trees"]
FIG_NAME = "frontier_combined.png"
# The cache tag hashes the effective batch (16 on every GPU), not the per-device batch.
FT_TAG = hashlib.sha1(json.dumps([FT_EPOCHS, FT_LR, 16, FT_WARMUP, LORA_R, LORA_ALPHA,
                                  LORA_DROPOUT, LABEL, SMOKE]).encode()).hexdigest()[:8]
'''

LORA3B = '''
m = "Qwen/Qwen2.5-Coder-3B-Instruct"
th = text_hash(build_texts(load_tok(m), MAX_LEN))
seeds = []
for seed in FT_SEEDS:
    paths = [CACHE_DIR / f"ft_{slug(m)}_{FT_TAG}_{FOLD_HASH}_{th}_s{seed}_f{f}.npy" for f in range(len(FOLDS))]
    if all(p.exists() for p in paths):
        p = np.zeros(H)
        for (tr, te), path in zip(FOLDS, paths):
            p[te] = np.load(path)
        seeds.append(p)
        print(f"LoRA 3B seed {seed}: AUC {roc_auc_score(Y, p):.3f}")
    else:
        print(f"LoRA 3B seed {seed}: not all folds cached, left out")
if seeds:
    name = f"LoRA 3B ({len(seeds)} seeds)"
    SEED_SCORES[name] = seeds
    SCORES[name] = np.mean([pd.Series(p).rank().to_numpy() for p in seeds], axis=0)
    FAMILY[name] = ("LoRA", "3B")
'''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=str(REPO / "results/selector50_llm_results/cache"))
    ap.add_argument("--nonllm", default=str(REPO / "results/selector50_nonllm/nonllm_oof_scores.csv"))
    ap.add_argument("--out", default=str(REPO / "results/selector50_combined"))
    a = ap.parse_args()
    os.chdir(REPO)
    os.environ["SELECTOR_OUT"] = a.out
    import matplotlib
    matplotlib.use("Agg")

    nb = json.load(open(REPO / "notebooks/selector50_llm_a100.ipynb"))
    cells = [(i, "".join(c["source"])) for i, c in enumerate(nb["cells"]) if c["cell_type"] == "code"]
    g = {"__name__": "__main__"}
    for i, src in cells:
        if i == 2:
            src = src.replace("raise RuntimeError(", "print(")      # the kernel check is for the notebook
        print(f"\n######## cell {i}")
        exec(src, g)
        if i == 2:
            g["NONLLM_OOF"] = Path(a.nonllm)
            g["CACHE_DIR"] = Path(a.cache)
            empty = Path(a.out) / "cache"
            if empty.exists() and not any(empty.iterdir()):
                empty.rmdir()                                    # made by the cell; the cache is elsewhere
        if i == 5:
            exec(OVERRIDE, g)
            assert g["FT_TAG"] == "06772646", g["FT_TAG"]
        if i == 25:
            exec(LORA3B, g)


if __name__ == "__main__":
    main()
