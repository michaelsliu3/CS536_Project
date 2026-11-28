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

It reads the selectors' input file (hunk_outcomes.json, from `run_selector185.py inputs`), the
run's saved reviews and the selectors' fold of each hunk, prints the tables and writes
selector_loop.json next to the selector scores.
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
    values = [statistic(np.concatenate([members[name] for name in rng.choice(names, size=len(names))])) for _ in range(draws)]
    low, high = np.nanpercentile(values, [2.5, 97.5])
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
                 draws: int = 500) -> dict:
    """`selector`: a selector's score per hunk; with it, the loop is also run from the start that selector chooses."""
    hunks, top = len(data["hunks"]), len(data["levels"]) - 1
    scores = [gate_scores(data, level) for level in range(top)]
    report = {"hunks": hunks, "prs": data["prs"], "references": data["references"], "levels": data["levels"], "gates": [], "rows": [],
              "start_selector": selector_name, "start_spend": spend}
    for level in range(top):
        y = adds_catch(data, level)
        gate = {"from": data["levels"][level], "to": data["levels"][level + 1], "adds_a_catch": int(y.sum()),
                "auc": [auc(y, scores[level]), *interval(data["repo"], lambda i, s=scores[level], t=y: auc(t[i], s[i]))],
                "auc_changed_lines_alone": auc(y, data["lines"])}
        for inputs in ("facts", "review"):
            gate[f"auc_{inputs}_only"] = auc(y, gate_scores(data, level, inputs))
        report["gates"].append(gate)

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
    return report


def print_report(report: dict) -> None:
    print(f"{report['hunks']:,} hunks in {report['prs']} pull requests; {report['references']:,} reference comments")
    print("\nThe gate: how well its score ranks the hunks where the next level adds a catch (AUC, 0.5 is a coin flip)")
    for gate in report["gates"]:
        a = gate["auc"]
        print(f"   after {SHORT[gate['from']]:11s} -> {SHORT[gate['to']]:9s} adds a catch on {gate['adds_a_catch']:4d} hunks; AUC {a[0]:.3f} [{a[1]:.3f}, {a[2]:.3f}]"
              f" | facts before the review only {gate['auc_facts_only']:.3f} | the review's output only {gate['auc_review_only']:.3f}"
              f" | changed lines alone {gate['auc_changed_lines_alone']:.3f}")
    base = report["rows"][0]
    begun = None
    for r in report["rows"]:
        if r["start"] != begun:
            begun = r["start"]
            title = ("Every hunk starts with no context" if begun == "no context" else
                     f"The selector chooses the start ({report['start_selector']}, {report['start_spend']:.0%} of the function level's extra tokens)")
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--outcomes", type=Path, default=OUTCOMES, help="the selectors' input file, hunk_outcomes.json")
    parser.add_argument("--run", type=Path, default=RUN, help="the run folder that holds reviews/")
    parser.add_argument("--folds", type=Path, default=FOLDS, help="the selectors' scores file: each hunk's fold and each selector's score")
    parser.add_argument("--shares", type=float, nargs="+", default=[0.02, 0.05, 0.1, 0.2], help="shares of hunks that get a second review")
    parser.add_argument("--start-selector", default="zero-shot 7B", help="the selector that chooses the start, by its column in the scores file; '' for none")
    parser.add_argument("--start-spend", type=float, default=0.3, help="the share of the function level's extra tokens its choice spends (default 0.3)")
    parser.add_argument("--json", type=Path, help="where to write the report (default: selector_loop.json next to the scores file)")
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
    report = build_report(data, args.shares, selector, args.start_selector, args.start_spend)
    print_report(report)
    target = args.json or args.folds.with_name("selector_loop.json")
    target.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"\nwrote {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
