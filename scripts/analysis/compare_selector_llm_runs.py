"""Compare the M2 and A100 small-LLM selector runs on the same folds.

The two runs of the small-LLM selector (notebooks/selector_llm_colab.ipynb on a MacBook,
notebooks/selector_llm_colab_a100.ipynb on Colab) used different scikit-learn versions,
whose GroupKFold puts repositories into different folds. This script rebuilds the M2
run's folds and features by executing the M2 notebook's setup cells, then re-scores the
A100 run's cached zero-shot scores and embeddings on those folds. LoRA cannot be refit
without the model, so its A100 out-of-fold scores are used as saved (on the A100 folds).

Usage, from the project root, with the data folder of the M2 run:
    SELECTOR_DATA=/path/to/aacr_hunk_15% python scripts/analysis/compare_selector_llm_runs.py

Writes results/selector_llm_results_a100/same_folds/{auc,budget,bootstrap}.csv.
"""
import json
import sys
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

REPO = Path(__file__).resolve().parents[2]
M2 = REPO / "results/selector_llm"
A100 = REPO / "results/selector_llm_results_a100"
OUT = A100 / "same_folds"


def main():
    nb = json.load(open(REPO / "notebooks/selector_llm_colab.ipynb"))

    def src(i):
        return "".join(nb["cells"][i]["source"])

    g = {"__name__": "__main__"}
    exec(src(2), g)
    g["OUT_DIR"] = Path(tempfile.mkdtemp())          # keep the notebook's own outputs untouched
    for i in (3, 5, 7, 9, 11, 13, 15):               # setup, data, features, folds, tabular, helpers
        exec(src(i), g)

    hunk_ids = g["hunks"]["hunk"]
    oof_m2 = pd.read_csv(M2 / "llm_oof_scores.csv").set_index("hunk").loc[hunk_ids]
    oof_a100 = pd.read_csv(A100 / "llm_oof_scores.csv").set_index("hunk").loc[hunk_ids]
    XT, oof, PCA = g["XT"], g["oof_sklearn"], g["PCA"]

    def emb_scores(E, with_tab):
        def blocks(tr, te):
            pca = PCA(n_components=g["EMB_PCA"], random_state=0).fit(E[tr])
            a, b = pca.transform(E[tr]), pca.transform(E[te])
            return (np.hstack([XT[tr], a]), np.hstack([XT[te], b])) if with_tab else (a, b)
        return oof(blocks)

    def one(folder, prefix):
        found = sorted((folder / "cache").glob(prefix + "*.npy"))
        assert len(found) == 1, found
        return np.load(found[0])

    e05, e3 = one(M2, "emb_"), one(A100, "emb_")
    S = {
        "tabular logistic": g["SCORES"]["tabular logistic"],
        "zero-shot 1.5B": one(M2, "zs_"),
        "zero-shot 7B": one(A100, "zs_"),
        "embedding 0.5B": emb_scores(e05, False),
        "embedding 3B": emb_scores(e3, False),
        "embedding + tabular 0.5B": emb_scores(e05, True),
        "embedding + tabular 3B": emb_scores(e3, True),
        "LoRA 0.5B": oof_m2["score|LLM fine-tuned (LoRA)"].to_numpy(),
        "LoRA 3B (A100 folds)": oof_a100["score|LLM fine-tuned (LoRA)"].to_numpy(),
    }
    # The M2 run must be reproduced exactly, or the folds are not the M2 folds.
    assert np.allclose(S["tabular logistic"], oof_m2["score|tabular logistic"]), "tabular not reproduced"
    assert np.allclose(S["embedding 0.5B"], oof_m2["score|LLM embedding"]), "embedding not reproduced"

    g["SCORES"] = S
    g["SEED_SCORES"] = {"LLM fine-tuned (LoRA)": []}
    g["RUN_FINETUNE"] = False
    for i in (23, 25, 29):                           # AUC table, budget table, paired bootstrap
        exec(src(i), g)

    OUT.mkdir(parents=True, exist_ok=True)
    g["AUC"].to_csv(OUT / "auc.csv", index=False)
    g["BUDGET_TABLE"].to_csv(OUT / "budget.csv", index=False)
    g["COMPARE"].to_csv(OUT / "bootstrap.csv", index=False)
    print("written:", *sorted(p.name for p in OUT.glob("*.csv")))


if __name__ == "__main__":
    sys.exit(main())
