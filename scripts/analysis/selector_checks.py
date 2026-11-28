#!/usr/bin/env python3
"""Can a selector tell, before any review, which hunks need more than no context? Three steps on
a finished per-hunk run. Calls no model.

    .venv/bin/python scripts/analysis/selector_checks.py rows  --profile cloud-hunk-kept
    .venv/bin/python scripts/analysis/selector_checks.py curve --profile cloud-hunk-kept
    .venv/bin/python scripts/analysis/selector_checks.py text  --profile cloud-hunk-kept

rows   One row per reviewed hunk: facts known before review (sizes, trace counts, each level's
       input tokens) and, per level, which reference comments the hunk's findings covered. It
       also prints how often each wider level covers more or fewer references than the first.
curve  Learning curve of the ranking score (AUC) for "a wider level covers more than the first
       level", from the sizes and counts. Repositories are split into folds; each fold is ranked
       by a model trained on 25, 50, 75 or 100% of the other folds' repositories; the folds'
       rankings are pooled; this is repeated over random splits. A control with shuffled labels
       gives the chance level. About a quarter of an hour on 900 hunks.
text   The same question from the hunk's own text (the first level's text and the file path) as
       word or character counts, at full training size.

Files go to <data root>/selector_test/ (git-ignored), named after the run:
rows_<run>.json, learning_curve_<run>.json and text_check_<run>.json. `curve` and `text` need
scikit-learn.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
import warnings
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.review_outcomes.profiles import read_notebook_profile  # noqa: E402

FRACTIONS = [0.25, 0.5, 0.75, 1.0]
FILE_LEVEL = "H03_file"


# ---------- rows ----------

def _hop(stats: dict, name: str) -> int:
    value = stats.get(name) or {}
    return int(value.get("1", value.get(1, 0)) or 0) if isinstance(value, dict) else 0


def build_rows(contexts: Path, run: Path, levels: list[str]) -> dict:
    """{"rows": one per reviewed hunk, "refs": the references of each PR, "levels": levels,
    "left_out": hunks with a level that has no review}. A hunk that could not be reviewed at some
    level (for example too long to send) has no judgment there; it is left out of the rows."""
    with (run / "hunk_table.csv").open(newline="", encoding="utf-8") as handle:
        hunk_table = {(r["entry"], r["level"]): r for r in csv.DictReader(handle)}
    reviewed = {entry for entry, _ in hunk_table}
    missing = sorted(entry for entry in reviewed if any((entry, level) not in hunk_table for level in levels))
    if missing:
        raise SystemExit(f"{len(missing)} hunks lack one of {levels}, e.g. {missing[:3]}")
    left_out = sorted(entry for entry in reviewed
                      if any((hunk_table[(entry, level)].get("review_status") or "ok") != "ok" for level in levels))
    reviewed -= set(left_out)
    references: dict[str, dict] = {}
    with (run / "coverage.csv").open(newline="", encoding="utf-8") as handle:
        for c in csv.DictReader(handle):
            if c["level"] == levels[0]:
                references.setdefault(c["entry"], {})[c["reference"]] = {
                    "context": c.get("context"), "category": c.get("category"), "path": c.get("path"),
                    "human": c.get("is_ai_comment") == "False"}
    rows = []
    with contexts.open(encoding="utf-8") as handle:
        for line in handle:
            entry = json.loads(line)
            per_file = Counter(h["file"] for h in entry["hunks"])
            for h in entry["hunks"]:
                if h["id"] not in reviewed:
                    continue
                st = h["stats"]
                row = {"hunk": h["id"], "pr": entry["id"], "language": entry["language"], "file": h["file"],
                       "ext": os.path.splitext(h["file"])[1].lower(), "pr_hunks": len(entry["hunks"]),
                       "file_hunks": per_file[h["file"]], "added": st.get("added_lines", 0), "removed": st.get("removed_lines", 0),
                       "diff_chars": st.get("diff_chars", 0), "changed_symbols": st.get("changed_symbols", 0),
                       "callees1": _hop(st, "callees_by_hop"), "callers1": _hop(st, "callers_by_hop"),
                       "tests1": _hop(st, "test_callers_by_hop"), "search_hits": st.get("search_hits", 0),
                       "new_start": h["new_start"], "new_file": h.get("old_file") is None and h["old_start"] == 0,
                       "deleted_file": bool(h.get("deleted_file")), "pr_refs": len(references.get(entry["id"], {})), "levels": {}}
                for level in levels:
                    cell = hunk_table[(h["id"], level)]
                    judged = json.loads((run / "judgments" / h["id"] / f"{level}.json").read_text(encoding="utf-8"))
                    row["levels"][level] = {"tokens": int(cell["input_tokens"]), "findings": int(cell["findings"]),
                                            "covered": sorted(judged["covered_indices"]), "matched": judged["matched_generated"],
                                            "out": int(cell["output_tokens"] or 0)}
                rows.append(row)
    if len(rows) != len(reviewed):
        raise SystemExit(f"{len(reviewed)} reviewed hunks in the table but {len(rows)} found in {contexts.name}")
    return {"rows": rows, "refs": references, "levels": levels, "left_out": left_out}


def covered_counts(rows: list[dict], levels: list[str]) -> dict[str, list[int]]:
    return {level: [len(r["levels"][level]["covered"]) for r in rows] for level in levels}


def labels(rows: list[dict], levels: list[str]) -> dict[str, list[int]]:
    """The selector's labels per hunk. "wider": some level after the first covers more references
    than the first. "file": the file level does (only when the run has that level)."""
    covered = covered_counts(rows, levels)
    first = covered[levels[0]]
    out = {"wider": [int(max(covered[lv][i] for lv in levels[1:]) > first[i]) for i in range(len(rows))]}
    if FILE_LEVEL in levels[1:]:
        out["file"] = [int(covered[FILE_LEVEL][i] > first[i]) for i in range(len(rows))]
    return out


def gains_losses(rows: list[dict], levels: list[str]) -> dict[str, tuple[int, int]]:
    """Per wider level: (hunks where it covers more references than the first level, hunks where fewer)."""
    covered = covered_counts(rows, levels)
    first = covered[levels[0]]
    return {lv: (sum(a > b for a, b in zip(covered[lv], first)), sum(a < b for a, b in zip(covered[lv], first)))
            for lv in levels[1:]}


def union_covered(rows: list[dict], choose) -> tuple[int, int]:
    """(references covered, input tokens) when each hunk is reviewed at the level `choose(row)`
    returns; a PR's references are the union over its hunks, as in the run's own table."""
    by_pr: dict[str, set] = {}
    tokens = 0
    for r in rows:
        level = choose(r)
        by_pr.setdefault(r["pr"], set()).update(r["levels"][level]["covered"])
        tokens += r["levels"][level]["tokens"]
    return sum(len(v) for v in by_pr.values()), tokens


def hindsight_level(row: dict, levels: list[str]) -> str:
    """The cheapest level among those that cover the most references on this hunk."""
    most = max(len(row["levels"][lv]["covered"]) for lv in levels)
    return min((lv for lv in levels if len(row["levels"][lv]["covered"]) == most), key=lambda lv: row["levels"][lv]["tokens"])


def cmd_rows(args, config, paths) -> int:
    table = build_rows(PROJECT_ROOT / config["contexts"], paths["run"], config["levels"])
    rows, levels = table["rows"], table["levels"]
    paths["rows"].parent.mkdir(parents=True, exist_ok=True)
    paths["rows"].write_text(json.dumps(table), encoding="utf-8")
    prs = {r["pr"] for r in rows}
    print(f"{len(rows)} hunks, {len(prs)} PRs, {sum(len(v) for v in table['refs'].values())} references -> {paths['rows']}")
    if table["left_out"]:
        print(f"  left out, a level has no review: {table['left_out']}")
    with (paths["run"] / "coverage.csv").open(newline="", encoding="utf-8") as handle:
        in_table = Counter(c["level"] for c in csv.DictReader(handle) if c["covered"] in ("True", "1"))
    for level in levels:
        mine, tokens = union_covered(rows, lambda r, level=level: level)
        note = "" if mine == in_table[level] else f"   <-- coverage.csv has {in_table[level]}"
        print(f"  {level}: covered {mine}, input tokens {tokens:,}{note}")
    best, tokens = union_covered(rows, lambda r: hindsight_level(r, levels))
    some = len({(r["pr"], i) for r in rows for lv in levels for i in r["levels"][lv]["covered"]})
    print(f"  with hindsight (cheapest level that covers the most): covered {best}, input tokens {tokens:,}; "
          f"covered at some level {some}; picks {dict(Counter(hindsight_level(r, levels) for r in rows))}")
    for name, y in labels(rows, levels).items():
        print(f"  label {name!r}: {sum(y)} positive hunks, {sum(y) / len(prs):.2f} per PR, {sum(y) / len(rows):.1%} of reviewed hunks")
    for level, (more, fewer) in gains_losses(rows, levels).items():
        print(f"  {level} against {levels[0]}: more on {more} hunks, fewer on {fewer}")
    return 0


# ---------- curve and text ----------

def repository(pr: str) -> str:
    return "__".join(pr.split("__")[:-1])


def features(row: dict, levels: list[str], languages: list[str]) -> list[float]:
    """Facts known before any review: the hunk's size, its place in the PR, trace counts, each level's cost."""
    t = [row["levels"][lv]["tokens"] for lv in levels]
    path = row["file"].lower()
    steps = [max(0, b - a) for a, b in zip(t, t[1:])]  # a level is at times a little shorter than the one before
    return [row["added"], row["removed"], row["diff_chars"], row["pr_hunks"], row["file_hunks"], row["changed_symbols"],
            row["callees1"], row["callers1"], row["tests1"], row["search_hits"], row["new_start"], float(bool(row["new_file"])),
            t[0], *steps, float(t[1] == t[0]), float(t[-1] == t[-2]),
            float("test" in path or "spec" in path)] + [float(row["language"] == lang) for lang in languages]


def _fold_scores(y, repos, seed: int, folds: int, fraction: float, fit_predict):
    """Out-of-fold scores with repositories as the unit, and the mean training size and positives."""
    import numpy as np
    order = np.random.default_rng(seed).permutation(sorted(set(repos)))
    groups = [order[i::folds] for i in range(folds)]
    scores, sizes, positives = np.zeros(len(y)), [], []
    for k, held in enumerate(groups):
        rest = [repo for j, group in enumerate(groups) if j != k for repo in group]
        used = rest[:max(1, math.ceil(fraction * len(rest)))]
        train, test = np.isin(repos, used), np.isin(repos, held)
        sizes.append(train.sum()); positives.append(y[train].sum())
        scores[test] = fit_predict(train, test, seed) if len(set(y[train])) == 2 else 0.0
    return scores, float(np.mean(sizes)), float(np.mean(positives))


def cmd_curve(args, config, paths) -> int:
    import numpy as np
    from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import FunctionTransformer, StandardScaler

    warnings.filterwarnings("ignore")
    table = json.loads(paths["rows"].read_text(encoding="utf-8"))
    rows, levels = table["rows"], table["levels"]
    repos = np.array([repository(r["pr"]) for r in rows])
    languages = sorted({r["language"] for r in rows})
    x = np.array([features(r, levels, languages) for r in rows], dtype=float)
    models = {
        "logistic regression": lambda seed: make_pipeline(FunctionTransformer(np.log1p), StandardScaler(),
                                                          LogisticRegression(C=0.3, class_weight="balanced", max_iter=3000)),
        "random forest": lambda seed: RandomForestClassifier(n_estimators=200, min_samples_leaf=3, class_weight="balanced_subsample",
                                                             random_state=seed, n_jobs=-1),
        "boosted trees": lambda seed: HistGradientBoostingClassifier(max_depth=3, learning_rate=0.06, max_iter=150,
                                                                     class_weight="balanced", random_state=seed),
    }
    out = {"run": paths["run"].name, "hunks": len(rows), "repositories": len(set(repos)), "prs": len({r["pr"] for r in rows}),
           "features": x.shape[1], "repeats": args.repeats, "folds": args.folds, "labels": {}}
    for label, values in labels(rows, levels).items():
        y = np.array(values)
        result = {"positives": int(y.sum()), "curve": {}, "shuffled": {}}
        for name, make in models.items():
            def run(target, seed, fraction, make=make):
                def fit_predict(train, test, seed):
                    return make(seed).fit(x[train], target[train]).predict_proba(x[test])[:, 1]
                scores, size, positives = _fold_scores(target, repos, seed, args.folds, fraction, fit_predict)
                return roc_auc_score(target, scores), size, positives
            result["curve"][name] = {}
            for fraction in FRACTIONS:
                runs = [run(y, seed, fraction) for seed in range(args.repeats)]
                aucs = np.array([r[0] for r in runs])
                result["curve"][name][str(fraction)] = {
                    "auc_mean": round(float(aucs.mean()), 4), "auc_sd": round(float(aucs.std(ddof=1)), 4),
                    "auc_low": round(float(np.percentile(aucs, 2.5)), 4), "auc_high": round(float(np.percentile(aucs, 97.5)), 4),
                    "train_hunks": round(float(np.mean([r[1] for r in runs])), 1),
                    "train_positives": round(float(np.mean([r[2] for r in runs])), 1)}
            shuffled = np.array([run(np.random.default_rng(1000 + seed).permutation(y), seed, 1.0)[0] for seed in range(args.repeats)])
            result["shuffled"][name] = {"auc_mean": round(float(shuffled.mean()), 4), "auc_sd": round(float(shuffled.std(ddof=1)), 4),
                                        "auc_high": round(float(np.percentile(shuffled, 97.5)), 4)}
            print(f"{label} | {name}: " + ", ".join(f"{int(float(f) * 100)}% {v['auc_mean']:.3f}±{v['auc_sd']:.3f}"
                                                     for f, v in result["curve"][name].items())
                  + f" | shuffled {result['shuffled'][name]['auc_mean']:.3f}±{result['shuffled'][name]['auc_sd']:.3f}", flush=True)
        out["labels"][label] = result
    paths["curve"].write_text(json.dumps(out, indent=1), encoding="utf-8")
    print("wrote", paths["curve"])
    return 0


def hunk_texts(contexts: Path, rows: list[dict], level: str) -> list[str]:
    """Per row: the file path and the level's text (the longest text field of the level)."""
    want, text = {r["hunk"] for r in rows}, {}
    with contexts.open(encoding="utf-8") as handle:
        for line in handle:
            entry = json.loads(line)
            for h in entry["hunks"]:
                if h["id"] in want:
                    body = max((v for v in h["levels"][level].values() if isinstance(v, str)), key=len, default="")
                    text[h["id"]] = h["file"] + "\n" + body
    return [text[r["hunk"]] for r in rows]


def cmd_text(args, config, paths) -> int:
    import numpy as np
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.metrics import roc_auc_score

    warnings.filterwarnings("ignore")
    table = json.loads(paths["rows"].read_text(encoding="utf-8"))
    rows, levels = table["rows"], table["levels"]
    texts = hunk_texts(PROJECT_ROOT / config["contexts"], rows, levels[0])
    repos = np.array([repository(r["pr"]) for r in rows])
    covered = covered_counts(rows, levels)
    targets = {"wider": np.array(labels(rows, levels)["wider"]),
               "covered at some level": np.array([int(max(covered[lv][i] for lv in levels) > 0) for i in range(len(rows))])}
    kinds = {"words": dict(analyzer="word", token_pattern=r"[A-Za-z_]+|[^\sA-Za-z_]", ngram_range=(1, 2), min_df=2, sublinear_tf=True,
                           max_features=100000),
             "characters": dict(analyzer="char_wb", ngram_range=(3, 5), min_df=3, sublinear_tf=True, max_features=50000)}

    def auc(target, kind, seed):
        def fit_predict(train, test, seed):
            vectorizer = TfidfVectorizer(**kind)
            x_train = vectorizer.fit_transform([t for t, keep in zip(texts, train) if keep])
            model = LogisticRegression(C=1.0, max_iter=3000, class_weight="balanced").fit(x_train, target[train])
            return model.predict_proba(vectorizer.transform([t for t, keep in zip(texts, test) if keep]))[:, 1]
        return roc_auc_score(target, _fold_scores(target, repos, seed, args.folds, 1.0, fit_predict)[0])

    out = {"run": paths["run"].name, "hunks": len(rows), "repositories": len(set(repos)), "repeats": args.repeats, "folds": args.folds,
           "median_chars": int(np.median([len(t) for t in texts])), "labels": {}}
    for label, y in targets.items():
        out["labels"][label] = {"positives": int(y.sum())}
        for name, kind in kinds.items():
            aucs = np.array([auc(y, kind, seed) for seed in range(args.repeats)])
            shuffled = np.array([auc(np.random.default_rng(1000 + seed).permutation(y), kind, seed) for seed in range(args.repeats)])
            out["labels"][label][name] = {"auc_mean": round(float(aucs.mean()), 4), "auc_sd": round(float(aucs.std(ddof=1)), 4),
                                          "shuffled_mean": round(float(shuffled.mean()), 4),
                                          "shuffled_sd": round(float(shuffled.std(ddof=1)), 4)}
            print(f"{label} ({int(y.sum())} of {len(y)}) | text as {name}: {aucs.mean():.3f}±{aucs.std(ddof=1):.3f}"
                  f" | shuffled {shuffled.mean():.3f}±{shuffled.std(ddof=1):.3f}", flush=True)
    paths["text"].write_text(json.dumps(out, indent=1), encoding="utf-8")
    print("wrote", paths["text"])
    return 0


def run_paths(config: dict) -> dict[str, Path]:
    """Where a profile's run is and where this script's files go."""
    folder = (PROJECT_ROOT / config["contexts"]).parent / "selector_test"
    name = config["run_name"]
    return {"run": PROJECT_ROOT / config["out_root"] / name, "rows": folder / f"rows_{name}.json",
            "curve": folder / f"learning_curve_{name}.json", "text": folder / f"text_check_{name}.json"}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    for name, func, text in (("rows", cmd_rows, "one row per reviewed hunk, and the gains and losses of each level"),
                             ("curve", cmd_curve, "learning curve from sizes and counts"),
                             ("text", cmd_text, "the same question from the hunk's text")):
        p = sub.add_parser(name, help=text)
        p.add_argument("--profile", default="cloud-hunk-kept", help="a per-hunk notebook profile (default cloud-hunk-kept)")
        p.add_argument("--folds", type=int, default=5, help="folds of repositories (default 5)")
        p.add_argument("--repeats", type=int, default=40 if name == "curve" else 10, help="random splits")
        p.set_defaults(func=func)
    args = parser.parse_args(argv)
    config = read_notebook_profile(args.profile)
    return args.func(args, config, run_paths(config))


if __name__ == "__main__":
    sys.exit(main())
