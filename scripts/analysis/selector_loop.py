#!/usr/bin/env python3
"""A feedback loop on top of the context selector, simulated on saved reviews. Calls no model.

    .venv/bin/python scripts/analysis/selector_loop.py
    .venv/bin/python scripts/analysis/selector_loop.py --shares 0.02 0.05 0.1

The loop: every hunk is first reviewed at its starting level. After each review a gate looks at
that review and at the facts known before it, and gives the hunk a score: the chance that a
review at the next level would catch a reference comment the reviews so far missed. When the
score is at least the price, the hunk is reviewed again at the next level and the findings of all
its reviews are kept together. This repeats up to the widest level. A high price makes extra
reviews rare.

Two starts are run. In the first every hunk starts at the lowest level (no code context). In the
second the selector chooses the start: the hunks it ranks highest start at the function level,
by the selector runs' budget rule (--start-selector, --start-spend: by default the 7B model asked
directly and 30% of the function level's extra tokens, the setting of the selector write-ups,
fixed before this was run). A hunk that starts higher is not reviewed at the levels below. The
gates are the same in both; they were trained for hunks that start at the lowest level.

No new review is run: a hunk's review at a wider level is the one the run already saved for it.
Each of those was made without seeing the earlier review, so this is the loop in which every
round is a fresh request.

The gate never sees a reference comment at the time it decides. It is trained on other
repositories (the selector runs' five repository folds) with the label "the next level adds a
catch", and its inputs are the pre-review facts of scripts/analysis/selector_checks.py and the
review just made: the number of findings, findings off the hunk, output tokens, characters,
question marks, and findings that say code cannot be seen or ask the author to check.

Every request is paid in full: the input and output tokens of every review a hunk gets, at the
price list of the run's tables. The loop is compared at equal cost with two ways of spending the
same extra dollars on second reviews at the function level: on hunks drawn at random (500 draws)
and on the largest hunks first. It is also compared with a gate that knows the answer (hindsight).
The gate's ranking is shown for three sets of inputs, to tell what the first review adds to it.

--gate-scores puts another gate in the trained one's place: a file with a score per hunk and
level, such as the one scripts/analysis/selector_gate_llm.py writes when a code model is asked
whether a second review would pay off. The price is then compared with that score.

It reads the selectors' input file (hunk_outcomes.json, from `run_selector185.py inputs`), the
run's saved reviews and the selectors' fold of each hunk, prints the tables and writes
selector_loop.json next to the selector scores (selector_loop_<file name>.json with --gate-scores).
The run with the trained gate also draws docs/figures/selector_loop_185/loop.png; --compare adds
the gate of a saved --gate-scores report to it. The write-up is docs/selector_loop_185.md.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import warnings
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import selector_checks as checks  # noqa: E402

LEVELS = ["H01_min", "H02_function", "H03_file", "H04_callees1"]
SHORT = {"H01_min": "no context", "H02_function": "function", "H03_file": "file", "H04_callees1": "callees"}
OUTCOMES = PROJECT_ROOT / "data/aacr_hunk_selector_files/hunk_outcomes.json"
RUN = PROJECT_ROOT / "data/aacr_hunk/outcomes/qwen3.8-27b_aacr-hunk_kept4"
FOLDS = PROJECT_ROOT / "results/selector185_llm/llm_oof_scores.csv"
FIGURE = PROJECT_ROOT / "docs/figures/selector_loop_185/loop.png"
WEIGHTS = {"blocking": 5.0, "should_fix": 2.0, "nit": 0.5}
PRICES = {"input": 2.00, "output": 10.00}                         # dollars per million tokens
# What a finding says, on its text (the patterns of scripts/analysis/confirmation_run_report.py).
CANNOT_SEE = re.compile(r"not (?:shown|visible|defined|included|present|provided|declared)(?: \w+){0,3} (?:in|within) "
                        r"(?:the|this) (?:diff|hunk|visible|provided|snippet|shown)|visible (?:portion|scope|part|code)"
                        r"|(?:in|within) the (?:visible|provided|shown) (?:portion|scope|code|diff|snippet)"
                        r"|cannot (?:see|verify|determine|tell)|can't (?:see|verify|tell)|without seeing|not shown"
                        r"|outside (?:of )?(?:the|this) (?:diff|hunk)|is not visible|isn't visible"
                        r"|not (?:visible|shown) here|from (?:the|this) diff alone|based on the diff alone", re.I)
ASKS_TO_CHECK = re.compile(r"\b(?:ensure|verify|confirm|make sure|double[- ]check|please check|check) (?:that|whether|if)\b"
                           r"|\bplease (?:verify|confirm|ensure)\b|\bworth (?:verifying|checking|confirming)\b", re.I)


# ---------- reading ----------

def review_facts(review: dict) -> dict:
    """What one saved review holds that the gate and the bill need."""
    notes = [finding.get("note") or "" for finding in review["parsed"]["findings"]]
    return {"in": review["reply"]["prompt_tokens"], "out": review["reply"]["completion_tokens"], "findings": len(notes),
            "off_hunk": len(review["parsed"].get("off_hunk_findings") or []), "chars": sum(map(len, notes)),
            "questions": sum(note.count("?") for note in notes), "cannot_see": sum(bool(CANNOT_SEE.search(note)) for note in notes),
            "asks_to_check": sum(bool(ASKS_TO_CHECK.search(note)) for note in notes)}


def load(outcomes: Path, run: Path, folds: Path) -> dict:
    """The rows of the selectors' input file with, per hunk and level, the references its review
    covers and the facts of that review; each reference's weight; each hunk's fold."""
    saved = json.loads(Path(outcomes).read_text(encoding="utf-8"))
    rows, levels = saved["rows"], saved["levels"]
    keys = sorted((pr, int(index)) for pr, items in saved["refs"].items() for index in items)
    position = {key: i for i, key in enumerate(keys)}
    weight = np.array([WEIGHTS.get(saved["refs"][pr][str(index)].get("tier"), 0.0) for pr, index in keys])
    covered = [[frozenset(position[(row["pr"], index)] for index in row["levels"][level]["covered"]) for level in levels] for row in rows]
    reviews = [[review_facts(json.loads((Path(run) / "reviews" / row["hunk"] / f"{level}.json").read_text(encoding="utf-8")))
                for level in levels] for row in rows]
    with Path(folds).open(newline="", encoding="utf-8") as handle:
        fold_of = {line["hunk"]: int(line["fold"]) for line in csv.DictReader(handle)}
    languages = sorted({row["language"] for row in rows})
    each = np.zeros((len(rows), len(levels), len(keys)), dtype=bool)       # each[h, k]: covered by hunk h's review at level k
    for h, cells in enumerate(covered):
        for k, cell in enumerate(cells):
            each[h, k, list(cell)] = True
    price = np.array([[(r["in"] * PRICES["input"] + r["out"] * PRICES["output"]) / 1e6 for r in row] for row in reviews])
    return {"levels": levels, "hunks": [row["hunk"] for row in rows], "prs": len({row["pr"] for row in rows}),
            "repo": np.array([checks.repository(row["pr"]) for row in rows]), "fold": np.array([fold_of[row["hunk"]] for row in rows]),
            "base": np.array([checks.features(row, levels, languages) for row in rows], dtype=float),
            "lines": np.array([row["added"] + row["removed"] for row in rows], dtype=float),
            "tokens": np.array([[row["levels"][level]["tokens"] for level in levels] for row in rows], dtype=float),
            "covered": covered, "reviews": reviews, "weight": weight, "references": len(keys), "each": each, "price": price}


# ---------- the gate ----------

def adds_catch(data: dict, level: int) -> np.ndarray:
    """Per hunk: the review at level + 1 covers a reference that none of the reviews up to `level` covers."""
    return np.array([bool(cells[level + 1] - frozenset().union(*cells[:level + 1])) for cells in data["covered"]])


def gate_inputs(data: dict, level: int, inputs: str = "both") -> np.ndarray:
    """The gate's inputs: the pre-review facts ("facts"), the output of the review at `level`
    ("review"), or both."""
    names = ("findings", "off_hunk", "out", "chars", "questions", "cannot_see", "asks_to_check")
    review = np.array([[row[level][name] for name in names] for row in data["reviews"]], dtype=float)
    return {"facts": data["base"], "review": review, "both": np.hstack([data["base"], review])}[inputs]


def gate_scores(data: dict, level: int, inputs: str = "both") -> np.ndarray:
    """Out-of-fold chance that the next level adds a catch: a logistic regression trained on the
    other folds' repositories."""
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import FunctionTransformer, StandardScaler

    x, y, scores = gate_inputs(data, level, inputs), adds_catch(data, level).astype(int), np.zeros(len(data["hunks"]))
    for fold in np.unique(data["fold"]):
        test = data["fold"] == fold
        if len(set(y[~test])) < 2:
            continue
        model = make_pipeline(FunctionTransformer(np.log1p), StandardScaler(), LogisticRegression(C=0.3, max_iter=3000))
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            scores[test] = model.fit(x[~test], y[~test]).predict_proba(x[test])[:, 1]
    return scores


def auc(y, score) -> float:
    from scipy.stats import rankdata
    y = np.asarray(y, dtype=bool)
    if not y.any() or y.all():
        return float("nan")
    ranks = rankdata(score)
    return float((ranks[y].sum() - y.sum() * (y.sum() + 1) / 2) / (y.sum() * (~y).sum()))


def interval(groups: np.ndarray, statistic, draws: int = 5000, seed: int = 536) -> tuple[float, float]:
    """The middle 95% of `statistic(index)` over bootstrap draws of whole repositories."""
    rng = np.random.default_rng(seed)
    names = np.unique(groups)
    members = {name: np.flatnonzero(groups == name) for name in names}
    values = np.array([statistic(np.concatenate([members[name] for name in rng.choice(names, size=len(names))])) for _ in range(draws)])
    values = values[~np.isnan(values)]
    if not len(values):
        return float("nan"), float("nan")
    low, high = np.percentile(values, [2.5, 97.5])
    return float(low), float(high)


# ---------- the loop ----------

def selector_start(tokens: np.ndarray, score: np.ndarray, spend: float, level: int = 1) -> np.ndarray:
    """The selector's first choice, by the selector runs' budget rule: every hunk starts at level 0,
    and hunks go to `level` from the highest score down until a share `spend` of the extra tokens
    of sending every hunk there is spent."""
    extra = tokens[:, level] - tokens[:, 0]
    order = np.argsort(-np.asarray(score, dtype=float), kind="stable")
    count = int(np.searchsorted(np.cumsum(extra[order]), spend * extra.clip(min=0).sum() + 1e-9, side="right"))
    start = np.zeros(len(tokens), dtype=int)
    start[order[:count]] = level
    return start


def run_loop(scores: list[np.ndarray], price: float, start: np.ndarray | None = None) -> np.ndarray:
    """The last level each hunk is reviewed at: from its start it moves up one level while its
    score at the level it is on is at least the price."""
    end = np.zeros(len(scores[0]), dtype=int) if start is None else start.copy()
    for level, score in enumerate(scores):
        end[(end == level) & (score >= price)] += 1
    return end


def hindsight_loop(data: dict) -> np.ndarray:
    """The loop from the lowest level with a gate that knows the answer: up one level exactly
    when it adds a catch."""
    end = np.zeros(len(data["hunks"]), dtype=int)
    for level in range(len(data["levels"]) - 1):
        end[(end == level) & adds_catch(data, level)] += 1
    return end


def price_for(scores: np.ndarray, share: float) -> float:
    """The price at which this share of hunks gets a second review."""
    return float(np.quantile(scores, 1 - share)) if share > 0 else float("inf")


def evaluate(data: dict, end: np.ndarray, start: np.ndarray | None = None) -> dict:
    """What the reviews of levels start[h] to end[h] of every hunk cover together, and what they cost."""
    start = np.zeros(len(end), dtype=int) if start is None else start
    level = np.arange(len(data["levels"]))[None, :]
    used = (level >= start[:, None]) & (level <= end[:, None])
    found = (data["each"] & used[:, :, None]).any(axis=(0, 1))
    return {"covered": int(found.sum()), "recall": float(found.mean()), "score": float(found @ data["weight"] / data["weight"].sum()),
            "requests": int(used.sum()), "extra_requests": int((end - start).sum()), "hunks_with_more": int((end > start).sum()),
            "starts": [int((start == k).sum()) for k in range(len(data["levels"]))],
            "depths": [int((end == k).sum()) for k in range(len(data["levels"]))], "cost_per_pr": float((data["price"] * used).sum()) / data["prs"]}


def second_reviews(data: dict, order: np.ndarray, dollars: float, start: np.ndarray | None = None) -> np.ndarray:
    """One more review, at the level above its start, for the hunks in this order until `dollars`
    are spent. Returns each hunk's last level."""
    start = np.zeros(len(order), dtype=int) if start is None else start
    order = order[start[order] < len(data["levels"]) - 1]
    price = data["price"][order, start[order] + 1]
    taken = int(np.searchsorted(np.cumsum(price), dollars + 1e-12, side="right"))
    end = start.copy()
    end[order[:taken]] += 1
    return end


def at_equal_cost(data: dict, loop: dict, start: np.ndarray | None = None, draws: int = 500) -> dict:
    """What the loop's extra dollars buy when they go to one more review of hunks drawn at random,
    and of the largest hunks first, from the same start."""
    hunks = len(data["hunks"])
    start = np.zeros(hunks, dtype=int) if start is None else start
    dollars = (loop["cost_per_pr"] - evaluate(data, start, start)["cost_per_pr"]) * data["prs"]
    drawn = [evaluate(data, second_reviews(data, np.random.default_rng(seed).permutation(hunks), dollars, start), start) for seed in range(draws)]
    covered = np.array([r["covered"] for r in drawn])
    largest = evaluate(data, second_reviews(data, np.argsort(-data["lines"], kind="stable"), dollars, start), start)
    return {"random_covered_mean": float(covered.mean()), "random_covered_p95": float(np.percentile(covered, 95)),
            "random_as_good": float((covered >= loop["covered"]).mean()), "random_hunks_with_more": float(np.mean([r["hunks_with_more"] for r in drawn])),
            "largest_first_covered": largest["covered"], "largest_first_hunks_with_more": largest["hunks_with_more"]}


def build_report(data: dict, shares: list[float], selector: np.ndarray | None = None, selector_name: str = "", spend: float = 0.3,
                 draws: int = 500, gate: list[np.ndarray] | None = None, gate_name: str = "") -> dict:
    """`selector`: a selector's score per hunk; with it, the loop is also run from the start that
    selector chooses. `gate`: a score per hunk for each level below the widest, used in place of
    the trained gate."""
    hunks, top = len(data["hunks"]), len(data["levels"]) - 1
    scores = gate if gate is not None else [gate_scores(data, level) for level in range(top)]
    report = {"hunks": hunks, "prs": data["prs"], "references": data["references"], "levels": data["levels"], "gates": [], "rows": [],
              "start_selector": selector_name, "start_spend": spend,
              "gate": gate_name or ("given scores" if gate is not None else "logistic regression on the hunk's facts and the review's output")}
    for level in range(top):
        y = adds_catch(data, level)
        entry = {"from": data["levels"][level], "to": data["levels"][level + 1], "adds_a_catch": int(y.sum()),
                 "auc": [auc(y, scores[level]), *interval(data["repo"], lambda i, s=scores[level], t=y: auc(t[i], s[i]))],
                 "auc_changed_lines_alone": auc(y, data["lines"]), "share_above_zero": float((scores[level] > 0).mean())}
        if gate is None:
            for inputs in ("facts", "review"):
                entry[f"auc_{inputs}_only"] = auc(y, gate_scores(data, level, inputs))
        else:                                                      # a given gate is set beside the trained one
            trained = gate_scores(data, level)
            entry["auc_trained_gate"] = auc(y, trained)
            entry["auc_minus_trained_gate"] = [auc(y, scores[level]) - auc(y, trained),
                                               *interval(data["repo"], lambda i, s=scores[level], t=y, o=trained: auc(t[i], s[i]) - auc(t[i], o[i]))]
            entry["above_zero_where_it_adds"] = float((scores[level][y] > 0).mean()) if y.any() else None
            entry["above_zero_elsewhere"] = float((scores[level][~y] > 0).mean()) if not y.all() else None
        if selector is not None:                                   # the start selector's own score, made before any review
            entry["auc_start_selector"] = auc(y, selector)
        report["gates"].append(entry)

    def row(begin: str, name: str, end: np.ndarray, start: np.ndarray, price=None, baseline: bool = False) -> dict:
        entry = {"start": begin, "policy": name, "price": price, **evaluate(data, end, start)}
        if baseline:
            entry["equal_cost"] = at_equal_cost(data, entry, start, draws)
        return entry

    prices = [(share, price_for(scores[0], share)) for share in shares]     # set on the plain loop, used for both starts
    starts = [("no context", np.zeros(hunks, dtype=int))]
    if selector is not None:
        starts.append(("selector", selector_start(data["tokens"], selector, spend)))
    for begin, start in starts:
        report["rows"].append(row(begin, "no loop", start, start))
        for share, price in prices:
            report["rows"].append(row(begin, f"loop at the price that sends {share:.0%} of hunks up from no context", run_loop(scores, price, start), start,
                                      price, baseline=True))
    zero = np.zeros(hunks, dtype=int)
    report["rows"].append(row("no context", "loop with a gate that knows the answer (hindsight)", hindsight_loop(data), zero))
    report["rows"].append(row("no context", "every level for every hunk", np.full(hunks, top), zero))
    report["rows"].append(row("function", "the function level for every hunk, no loop", np.ones(hunks, dtype=int), np.ones(hunks, dtype=int)))
    return report


def print_report(report: dict) -> None:
    print(f"{report['hunks']:,} hunks in {report['prs']} pull requests; {report['references']:,} reference comments")
    print(f"\nThe gate ({report['gate']}): how well its score ranks the hunks where the next level adds a catch (AUC, 0.5 is a coin flip)")
    for gate in report["gates"]:
        a = gate["auc"]
        line = f"   after {SHORT[gate['from']]:11s} -> {SHORT[gate['to']]:9s} adds a catch on {gate['adds_a_catch']:4d} hunks; AUC {a[0]:.3f} [{a[1]:.3f}, {a[2]:.3f}]"
        if "auc_facts_only" in gate:
            line += f" | facts before the review only {gate['auc_facts_only']:.3f} | the review's output only {gate['auc_review_only']:.3f}"
        else:
            d = gate["auc_minus_trained_gate"]
            line += (f" | the trained gate {gate['auc_trained_gate']:.3f}, difference {d[0]:+.3f} [{d[1]:+.3f}, {d[2]:+.3f}]"
                     f" | score above zero (a Yes) for {gate['share_above_zero']:.1%} of hunks")
            if gate["above_zero_where_it_adds"] is not None and gate["above_zero_elsewhere"] is not None:
                line += f" ({gate['above_zero_where_it_adds']:.1%} where it adds a catch, {gate['above_zero_elsewhere']:.1%} elsewhere)"
        if "auc_start_selector" in gate:
            line += f" | the start selector's score before any review {gate['auc_start_selector']:.3f}"
        print(line + f" | changed lines alone {gate['auc_changed_lines_alone']:.3f}")
    base = report["rows"][0]
    begun = None
    for r in report["rows"]:
        if r["start"] != begun:
            begun = r["start"]
            title = {"no context": "Every hunk starts with no context", "function": "For reference",
                     "selector": f"The selector chooses the start ({report['start_selector']}, {report['start_spend']:.0%} of the function "
                                 f"level's extra tokens)"}[begun]
            print(f"\n{title}\n{'policy':66s}{'price':>7s}{'start above none':>18s}{'reviewed again':>16s}{'extra reviews':>15s}{'covered':>9s}{'score':>7s}"
                  f"{'cost/PR':>9s}{'x':>6s}   same dollars on random hunks: mean, 95th pct, as good | on the largest hunks")
        price = f"{r['price']:.2f}" if r["price"] not in (None, float("inf")) else ""
        line = (f"{r['policy']:66s}{price:>7s}{report['hunks'] - r['starts'][0]:>18d}{r['hunks_with_more']:>16d}{r['extra_requests']:>15d}"
                f"{r['covered']:>9d}{r['score']:>7.3f}{r['cost_per_pr']:>9.3f}{r['cost_per_pr'] / base['cost_per_pr']:>6.2f}")
        if "equal_cost" in r:
            e = r["equal_cost"]
            line += f"   {e['random_covered_mean']:.1f}, {e['random_covered_p95']:.0f}, {e['random_as_good']:.0%} | {e['largest_first_covered']}"
        print(line)
        if r["hunks_with_more"]:
            print(f"{'':66s}   hunks ending at {', '.join(f'{SHORT[lv]} {n}' for lv, n in zip(report['levels'], r['depths']))}")


# ---------- figure ----------

def figure(report: dict, path: Path, other: dict | None = None, other_name: str = "") -> None:
    """Left: comments covered against cost for the loop from both starts, and for the same dollars
    spent on second reviews of random hunks and of the largest hunks. Right: how well the gate
    ranks the hunks at each step. `other`: the report of a run with another gate (--gate-scores),
    drawn beside the trained one."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from figure_style import AQUA, BLUE, DPI, INK, INK2, MUTED, ORANGE, SURFACE, style

    base = report["rows"][0]["cost_per_pr"]

    def path_of(rows: list[dict], begin: str) -> list[dict]:       # the start alone, then the loop at each price
        return [r for r in rows if r["start"] == begin and (r["policy"] == "no loop" or "equal_cost" in r)]

    plain, chosen = path_of(report["rows"], "no context"), path_of(report["rows"], "selector")
    extra = path_of(other["rows"], "no context") if other else []
    hindsight = next(r for r in report["rows"] if "hindsight" in r["policy"])
    function = next((r for r in report["rows"] if r["start"] == "function"), None)

    fig, (left, right) = plt.subplots(1, 2, figsize=(12.4, 4.7), dpi=DPI, facecolor=SURFACE, gridspec_kw={"width_ratios": [1.5, 1]})
    fig.subplots_adjust(left=0.06, right=0.975, top=0.83, bottom=0.25, wspace=0.42)

    style(left, "y")
    x = lambda r: r["cost_per_pr"] / base                          # noqa: E731
    for r in plain[1:]:                                            # what the same dollars buy without a gate
        e = r["equal_cost"]
        left.plot([x(r), x(r)], [e["random_covered_mean"], e["random_covered_p95"]], color=MUTED, linewidth=1.2, zorder=2)
        left.plot([x(r)], [e["random_covered_mean"]], marker="o", markersize=6, color=MUTED, markeredgecolor=SURFACE, markeredgewidth=1.2, zorder=3)
        left.plot([x(r)], [e["largest_first_covered"]], marker="D", markersize=7, markerfacecolor="none", markeredgecolor=INK2,
                  markeredgewidth=1.4, zorder=6)
    drawn = [(plain, BLUE), (chosen, AQUA), (extra, ORANGE)]
    for rows, color in drawn:
        if rows:
            left.plot([x(r) for r in rows], [r["covered"] for r in rows], color=color, linewidth=2, marker="o", markersize=7,
                      markeredgecolor=SURFACE, markeredgewidth=1.5, zorder=5)
    for r in plain[1:]:                                            # the share of hunks reviewed again, on the plain loop
        left.annotate(f"{r['hunks_with_more'] / report['hunks']:.0%}", (x(r), r["covered"]), xytext=(-5, 7), textcoords="offset points",
                      ha="right", va="bottom", fontsize=8, color=INK2)
    left.annotate(f"no loop: {plain[0]['covered']}", (x(plain[0]), plain[0]["covered"]), xytext=(7, -3), textcoords="offset points",
                  ha="left", va="top", fontsize=8, color=INK2)
    if function:
        left.plot([x(function)], [function["covered"]], marker="s", markersize=7, color=INK2, markeredgecolor=SURFACE, markeredgewidth=1.2, zorder=4)
        left.annotate(f"function level for every hunk: {function['covered']}", (x(function), function["covered"]), xytext=(8, -1),
                      textcoords="offset points", ha="left", va="center", fontsize=8, color=INK2)
    everything = [r for rows, _ in drawn for r in rows]
    low = min([r["covered"] for r in everything] + ([function["covered"]] if function else []))
    high = max(r["covered"] for r in everything)
    left.set_ylim(low - 0.08 * (high - low), high + 0.22 * (high - low))
    left.set_xlim(0.97, max(x(r) for r in everything) + 0.05)
    left.annotate(f"a gate that knows the answer: {hindsight['covered']} at {x(hindsight):.2f} times the cost (above this chart)",
                  (0.015, 0.965), xycoords="axes fraction", ha="left", va="top", fontsize=8, color=INK2)
    left.set_xlabel("cost, as a multiple of one review per hunk with no context", fontsize=8.5, color=INK2)
    left.set_ylabel(f"reference comments covered, of {report['references']:,}", fontsize=8.5, color=INK2)
    left.set_title("Comments covered against cost (labels: share of hunks reviewed again)", fontsize=9.5, color=INK, loc="left", pad=8)
    handles = [Line2D([], [], color=BLUE, linewidth=2, marker="o", markersize=7, markeredgecolor=SURFACE, label="loop from no context"),
               Line2D([], [], color=AQUA, linewidth=2, marker="o", markersize=7, markeredgecolor=SURFACE, label="loop from the selector's start")]
    if extra:
        handles.append(Line2D([], [], color=ORANGE, linewidth=2, marker="o", markersize=7, markeredgecolor=SURFACE,
                              label=f"loop with {other_name or other['gate']} as the gate"))
    handles += [Line2D([], [], color=MUTED, linewidth=1.2, marker="o", markersize=6, markeredgecolor=SURFACE,
                       label="same dollars on random hunks (average, to the 95th percentile)"),
                Line2D([], [], linestyle="none", marker="D", markersize=7, markerfacecolor="none", markeredgecolor=INK2, markeredgewidth=1.4,
                       label="same dollars on the largest hunks first")]
    left.legend(handles=handles, loc="upper left", bbox_to_anchor=(-0.02, -0.17), ncol=2, frameon=False, fontsize=8.5, labelcolor=INK2,
                handletextpad=0.5, columnspacing=1.4)

    style(right, "x")
    gates = report["gates"]
    marks = [("the gate", BLUE, "o", lambda g: g["auc"][0]), ("facts known before the review only", AQUA, "o", lambda g: g.get("auc_facts_only")),
             ("the review's output only", INK2, "o", lambda g: g.get("auc_review_only")), ("changed lines alone", MUTED, "D", lambda g: g["auc_changed_lines_alone"])]
    for k, gate in enumerate(gates):
        y0 = len(gates) - 1 - k
        right.plot(gate["auc"][1:], [y0 + 0.24, y0 + 0.24], color=BLUE, linewidth=1.5, zorder=2)
        for j, (_, color, marker, value) in enumerate(marks):
            if value(gate) is not None:
                right.plot([value(gate)], [y0 + 0.24 - 0.16 * j], marker=marker, markersize=7, color=color, markeredgecolor=SURFACE,
                           markeredgewidth=1.3, zorder=3)
        right.annotate(f"{gate['auc'][0]:.2f}", (gate["auc"][2], y0 + 0.24), xytext=(6, 0), textcoords="offset points", va="center",
                       fontsize=8, color=INK2)
        if other:
            given = other["gates"][k]["auc"]
            right.plot(given[1:], [y0 - 0.4, y0 - 0.4], color=ORANGE, linewidth=1.5, zorder=2)
            right.plot([given[0]], [y0 - 0.4], marker="o", markersize=7, color=ORANGE, markeredgecolor=SURFACE, markeredgewidth=1.3, zorder=3)
            right.annotate(f"{given[0]:.2f}", (given[2], y0 - 0.4), xytext=(6, 0), textcoords="offset points", va="center", fontsize=8, color=INK2)
    right.axvline(0.5, color=INK2, linewidth=1, linestyle=(0, (3, 2)), zorder=1)
    right.annotate("coin flip", (0.5, len(gates) - 0.42), xytext=(-4, 0), textcoords="offset points", ha="right", va="center", fontsize=8, color=INK2)
    right.set_yticks(range(len(gates)), [f"after {SHORT[g['from']]}:\n{SHORT[g['to']]} next" for g in reversed(gates)])
    right.tick_params(axis="y", labelsize=9, labelcolor=INK2)
    right.set_ylim(-0.7, len(gates) - 0.25)
    right.set_xlim(0.38, 0.84)
    right.set_xlabel("AUC for \"the next level adds a catch\"", fontsize=8.5, color=INK2)
    right.set_title("How well the gate picks the hunks that gain", fontsize=9.5, color=INK, loc="left", pad=8)
    keys = [Line2D([], [], color=color if j == 0 else "none", linewidth=1.5, marker=marker, markersize=7, markerfacecolor=color,
                   markeredgecolor=SURFACE, label=label) for j, (label, color, marker, _) in enumerate(marks)]
    if other:
        keys.append(Line2D([], [], color=ORANGE, linewidth=1.5, marker="o", markersize=7, markeredgecolor=SURFACE, label=other_name or other["gate"]))
    right.legend(handles=keys, loc="upper center", bbox_to_anchor=(0.4, -0.17), ncol=2, frameon=False, fontsize=8.5, labelcolor=INK2,
                 handletextpad=0.4, columnspacing=1.0)
    fig.suptitle("A priced loop adds comments at a cost, about as many as second reviews of the largest hunks", fontsize=11.5, color=INK,
                 x=0.02, ha="left", y=0.965)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI, facecolor=SURFACE)
    plt.close(fig)


def read_gate(path: Path, data: dict) -> list[np.ndarray]:
    """A gate's score per hunk for each level below the widest, from a file with a column `gate|<level>` per level."""
    with Path(path).open(newline="", encoding="utf-8") as handle:
        lines = {line["hunk"]: line for line in csv.DictReader(handle)}
    scores = []
    for level in data["levels"][:-1]:
        values = [lines.get(hunk, {}).get(f"gate|{level}") for hunk in data["hunks"]]
        if any(value in (None, "") for value in values):
            raise SystemExit(f"{path} has no gate score at {level} for {sum(value in (None, '') for value in values)} hunks")
        scores.append(np.array(values, dtype=float))
    return scores


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--outcomes", type=Path, default=OUTCOMES, help="the selectors' input file, hunk_outcomes.json")
    parser.add_argument("--run", type=Path, default=RUN, help="the run folder that holds reviews/")
    parser.add_argument("--folds", type=Path, default=FOLDS, help="the selectors' scores file: each hunk's fold and each selector's score")
    parser.add_argument("--shares", type=float, nargs="+", default=[0.02, 0.05, 0.1, 0.2], help="shares of hunks that get a second review")
    parser.add_argument("--start-selector", default="zero-shot 7B", help="the selector that chooses the start, by its column in the scores file; '' for none")
    parser.add_argument("--start-spend", type=float, default=0.3, help="the share of the function level's extra tokens its choice spends (default 0.3)")
    parser.add_argument("--gate-scores", type=Path, help="a file with a gate score per hunk and level (selector_gate_llm.py), in place of the trained gate")
    parser.add_argument("--gate-name", default="", help="what the report calls that gate")
    parser.add_argument("--json", type=Path, help="where to write the report (default: selector_loop.json next to the scores file)")
    parser.add_argument("--figure", type=Path, default=FIGURE, help="the figure of docs/selector_loop_185.md; drawn by the run with the trained gate")
    parser.add_argument("--compare", type=Path, help="the saved report of a run with --gate-scores, to draw that gate in the figure too")
    parser.add_argument("--no-figure", action="store_true")
    args = parser.parse_args(argv)

    data = load(args.outcomes, args.run, args.folds)
    if data["levels"] != LEVELS:
        raise SystemExit(f"{args.outcomes} holds the levels {data['levels']}, not {LEVELS}")
    selector = None
    if args.start_selector:
        with args.folds.open(newline="", encoding="utf-8") as handle:
            column = {line["hunk"]: line.get(f"score|{args.start_selector}") for line in csv.DictReader(handle)}
        if any(column.get(hunk) in (None, "") for hunk in data["hunks"]):
            raise SystemExit(f"{args.folds} has no score of '{args.start_selector}' for some hunks")
        selector = np.array([float(column[hunk]) for hunk in data["hunks"]])
    gate = read_gate(args.gate_scores, data) if args.gate_scores else None
    report = build_report(data, args.shares, selector, args.start_selector, args.start_spend, gate=gate,
                          gate_name=args.gate_name or (args.gate_scores.stem if args.gate_scores else ""))
    print_report(report)
    target = args.json or args.folds.with_name(f"selector_loop_{args.gate_scores.stem}.json" if args.gate_scores else "selector_loop.json")
    target.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"\nwrote {target}")
    if not args.no_figure and gate is None:
        other = json.loads(args.compare.read_text(encoding="utf-8")) if args.compare else None
        figure(report, args.figure, other)
        print(f"drew {args.figure}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
