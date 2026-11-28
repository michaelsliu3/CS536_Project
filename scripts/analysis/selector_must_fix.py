#!/usr/bin/env python3
"""Do the selectors give more context to the hunks that carry a must-fix comment? The numbers and
the figure of docs/selector185_must_fix.md. Calls no model.

    .venv/bin/python scripts/analysis/selector_must_fix.py
    .venv/bin/python scripts/analysis/selector_must_fix.py --no-figure

It reads:

- the selectors' input file (--outcomes, written by `run_selector185.py inputs`): for every reviewed
  hunk, the reference comments its review covers at each level and the review's input tokens, and
  each reference comment's severity label;
- the selectors' out-of-fold scores (--scores, written by `run_selector185.py llm`);
- the run's profile (--profile), to place each reference comment on the hunks whose lines it sits
  on, by the scorer's own rule (src/review_outcomes/table.py). The counts are checked against the
  run's hunk_table.csv.

A must-fix hunk has a reference comment labelled `blocking` on its lines. That is known only
afterwards: no selector sees it. A selector gives a hunk more context in one of two ways. A
zero-shot model answers Yes (its score, log P(Yes) - log P(No), is above 0). Or, at a budget, the
hunk is among those the selector ranks highest until a share (--spend) of the extra tokens of an
upgrade level is spent, the rule of the selector notebooks.

It prints the tables, writes must_fix.json next to the scores and draws
docs/figures/selector185_must_fix/must_fix.png.

Every interval is the middle 95% of 5,000 bootstrap draws of repositories (seed 536). Random
choice is 1,000 random orders of the hunks (seeds 0 to 999).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

LEVELS = ["H01_min", "H02_function", "H03_file", "H04_callees1"]
SHORT = {"H01_min": "no context", "H02_function": "function", "H03_file": "file", "H04_callees1": "callees"}
OUTCOMES = PROJECT_ROOT / "data/aacr_hunk_selector_files/hunk_outcomes.json"
SCORES = PROJECT_ROOT / "results/selector185_llm/llm_oof_scores.csv"
FIGURE = PROJECT_ROOT / "docs/figures/selector185_must_fix/must_fix.png"
# The selectors shown, by their column in the scores file, and what the write-up calls them.
SELECTORS = {
    "zero-shot 7B": "7B model, zero-shot",
    "zero-shot 3B": "3B model, zero-shot",
    "zero-shot 1.5B": "1.5B model, zero-shot",
    "tabular, logistic": "hunk facts, logistic regression",
    "non-LLM: tabular, random forest": "hunk facts, random forest",
}


# ---------- reading ----------

def load_outcomes(path: Path) -> dict:
    """The selectors' input file as arrays. `cov[h, l, r]`: hunk h's review at level l covers
    reference r. `tok[h, l]`: the review's input tokens. `tier[r]`: the reference's severity label
    ("none" without one). `lines[h]`: the hunk's added and removed lines."""
    saved = json.loads(Path(path).read_text(encoding="utf-8"))
    levels, rows = saved["levels"], saved["rows"]
    keys = sorted((pr, int(index)) for pr, items in saved["refs"].items() for index in items)
    position = {key: i for i, key in enumerate(keys)}
    cov = np.zeros((len(rows), len(levels), len(keys)), dtype=bool)
    tok = np.zeros((len(rows), len(levels)))
    for h, row in enumerate(rows):
        for l, level in enumerate(levels):
            tok[h, l] = row["levels"][level]["tokens"]
            for index in row["levels"][level]["covered"]:
                cov[h, l, position[(row["pr"], index)]] = True
    return {"levels": levels, "hunks": [row["hunk"] for row in rows], "prs": [row["pr"] for row in rows],
            "repo": np.array([row["pr"].rsplit("__", 1)[0] for row in rows]), "cov": cov, "tok": tok,
            "tier": np.array([saved["refs"][pr][str(index)].get("tier") or "none" for pr, index in keys]),
            "lines": np.array([row["added"] + row["removed"] for row in rows], dtype=float), "reference": position}


def references_on_hunks(profile: str, data: dict) -> np.ndarray:
    """on[h, r]: reference r sits on hunk h's lines, by the scorer's rule. Checked against the
    count the run wrote for every hunk."""
    from src.review_outcomes import table
    from src.review_outcomes.profiles import dataset_path, read_notebook_profile

    config = read_notebook_profile(profile)
    place = {hunk: h for h, hunk in enumerate(data["hunks"])}
    cells = [cell for cell in table.discover_cells(PROJECT_ROOT / config["contexts"], levels=[config["levels"][0]]) if cell.entry in place]
    references = table.references_by_entry(dataset_path(config), cells)
    on = np.zeros((len(place), len(data["reference"])), dtype=bool)
    for cell in cells:
        for index, comment in enumerate(references[cell.pr]["comments"]):
            if comment.get("note") and table._in_hunk(comment, cell) and (cell.pr, index) in data["reference"]:
                on[place[cell.entry], data["reference"][(cell.pr, index)]] = True
    with (PROJECT_ROOT / config["out_root"] / config["run_name"] / "hunk_table.csv").open(newline="", encoding="utf-8") as handle:
        written = {row["entry"]: int(row["references_on_hunk"]) for row in csv.DictReader(handle) if row["level"] == config["levels"][0]}
    differ = [hunk for hunk, h in place.items() if on[h].sum() != written[hunk]]
    if differ:
        raise SystemExit(f"the references on {len(differ)} hunks differ from hunk_table.csv, for example {differ[:3]}")
    return on


def load_scores(path: Path, hunks: list[str]) -> dict[str, np.ndarray]:
    """Each selector's score per hunk, in the order of `hunks`."""
    with Path(path).open(newline="", encoding="utf-8") as handle:
        rows = {row["hunk"]: row for row in csv.DictReader(handle)}
    missing = [hunk for hunk in hunks if hunk not in rows]
    if missing:
        raise SystemExit(f"{path} has no score for {len(missing)} hunks, for example {missing[:3]}")
    names = [name for name in next(iter(rows.values())) if name.startswith("score|")]
    return {name[6:]: np.array([float(rows[hunk][name]) for hunk in hunks]) for name in names}


# ---------- the selectors' choices ----------

def order_of(score) -> np.ndarray:
    """Hunks from the highest score down; ties keep their order."""
    return np.argsort(-np.asarray(score, dtype=float), kind="stable")


def upgrade_choice(tok: np.ndarray, order: np.ndarray, level: int, spend: float) -> np.ndarray:
    """The level of each hunk when every hunk starts at level 0 and hunks go to `level` in this
    order until a share `spend` of the extra tokens of sending every hunk there is spent. The rule
    of the selector notebooks."""
    extra = tok[:, level] - tok[:, 0]
    budget = spend * extra.clip(min=0).sum()
    count = int(np.searchsorted(np.cumsum(extra[order]), budget + 1e-9, side="right"))
    choice = np.zeros(len(tok), dtype=int)
    choice[order[:count]] = level
    return choice


def covered(cov: np.ndarray, choice: np.ndarray, wanted: np.ndarray) -> int:
    """How many of the `wanted` references some hunk's review covers at its chosen level."""
    return int((cov[np.arange(len(choice)), choice, :].any(axis=0) & wanted).sum())


# ---------- statistics ----------

def auc(y, score) -> float:
    """The chance that a hunk with y ranks above a hunk without (ties count half); nan when one
    side is empty."""
    from scipy.stats import rankdata
    y = np.asarray(y, dtype=bool)
    if not y.any() or y.all():
        return float("nan")
    ranks = rankdata(score)
    return float((ranks[y].sum() - y.sum() * (y.sum() + 1) / 2) / (y.sum() * (~y).sum()))


def fifths(values) -> np.ndarray:
    """0 to 4: the fifth of each value by its rank; equal values are split in their given order."""
    ranks = np.argsort(np.argsort(np.asarray(values, dtype=float), kind="stable"), kind="stable")
    return (ranks * 5 // len(ranks)).astype(int)


def stratified_auc(y, score, strata) -> float:
    """The AUC of pairs taken inside one stratum only, pooled over the strata."""
    y, score, strata = np.asarray(y, dtype=bool), np.asarray(score, dtype=float), np.asarray(strata)
    won = pairs = 0.0
    for stratum in np.unique(strata):
        inside = strata == stratum
        n = y[inside].sum() * (~y[inside]).sum()
        if n:
            won += auc(y[inside], score[inside]) * n
            pairs += n
    return won / pairs if pairs else float("nan")


def interval(groups: np.ndarray, statistic, draws: int = 5000, seed: int = 536) -> tuple[float, float]:
    """The middle 95% of `statistic(index)` over bootstrap draws of whole groups (repositories);
    draws where it is nan are left out."""
    rng = np.random.default_rng(seed)
    names = np.unique(groups)
    members = {name: np.flatnonzero(groups == name) for name in names}
    values = []
    for _ in range(draws):
        value = statistic(np.concatenate([members[name] for name in rng.choice(names, size=len(names))]))
        if value == value:
            values.append(value)
    low, high = np.percentile(values, [2.5, 97.5])
    return float(low), float(high)


def share_gap(flag: np.ndarray, group: np.ndarray, index=slice(None)) -> float:
    """The share of `flag` among the hunks of `group` minus its share among the others."""
    flag, group = flag[index], group[index]
    if not group.any() or group.all():
        return float("nan")
    return float(flag[group].mean() - flag[~group].mean())


# ---------- the report ----------

def build_report(data: dict, on: np.ndarray, scores: dict[str, np.ndarray], spend: float = 0.3, draws: int = 5000,
                 random_orders: int = 1000, seed: int = 536) -> dict:
    cov, tok, repo, lines = data["cov"], data["tok"], data["repo"], data["lines"]
    hunks = len(tok)
    blocking, should = data["tier"] == "blocking", data["tier"] == "should_fix"
    must = (on & blocking).any(axis=1)                            # a blocking comment on the hunk's lines
    serious = (on & (blocking | should)).any(axis=1)
    commented = on.any(axis=1)
    gains = ((cov[:, 1:, :].any(axis=1) & ~cov[:, 0, :]) & blocking).any(axis=1)   # a wider level covers a blocking comment that no context misses
    shown = [name for name in SELECTORS if name in scores]
    report = {
        "hunks": hunks, "repositories": int(len(np.unique(repo))), "references": int(len(data["tier"])),
        "blocking_references": int(blocking.sum()), "blocking_on_a_hunk": int((on & blocking).any(axis=0).sum()),
        "hunks_with_a_comment": int(commented.sum()), "must_fix_hunks": int(must.sum()), "serious_hunks": int(serious.sum()),
        "hunks_where_context_gains_a_blocking_comment": int(gains.sum()), "spend": spend, "draws": draws, "names": SELECTORS,
    }

    # 1. Does the score rank the must-fix hunks higher?
    report["ranking"] = {}
    size_fifth = fifths(lines)
    for name, score in [*((name, scores[name]) for name in shown), ("changed lines", lines)]:
        report["ranking"][name] = {
            "must_fix": [auc(must, score), *interval(repo, lambda i, s=score: auc(must[i], s[i]), draws, seed)],
            "serious": [auc(serious, score), *interval(repo, lambda i, s=score: auc(serious[i], s[i]), draws, seed)],
            "must_fix_among_commented": [auc(must[commented], score[commented]),
                                         *interval(repo[commented], lambda i, s=score[commented], y=must[commented]: auc(y[i], s[i]), draws, seed)],
            "must_fix_within_size_fifths": stratified_auc(must, score, size_fifth),
        }

    # 2. A zero-shot model's own answer: Yes when the score is above 0.
    groups = {"all hunks": np.ones(hunks, dtype=bool), "must-fix comment on the hunk": must, "must-fix or should-fix comment": serious,
              "only milder comments": commented & ~serious, "no reference comment": ~commented}
    report["answers"] = {}
    for name in shown:
        if not name.startswith("zero-shot"):
            continue
        yes = scores[name] > 0
        report["answers"][name] = {
            "yes": {label: [int(yes[mask].sum()), int(mask.sum())] for label, mask in groups.items()},
            "other_hunks": [int(yes[~must].sum()), int((~must).sum())],
            "must_fix_minus_other": [share_gap(yes, must), *interval(repo, lambda i, y=yes: share_gap(y, must, i), draws, seed)],
        }

    # 3. At a budget: which hunks go up, and the blocking comments covered.
    report["budget"] = {}
    base = np.zeros(hunks, dtype=int)
    for level in range(1, len(data["levels"])):
        random = [upgrade_choice(tok, np.random.default_rng(k).permutation(hunks), level, spend) for k in range(random_orders)]
        random_must = np.array([(choice[must] > 0).mean() for choice in random])
        random_covered = np.array([covered(cov, choice, blocking) for choice in random])
        only_must = base.copy()
        only_must[must] = level
        at_none, at_level = cov[:, 0, :].any(axis=0) & blocking, cov[:, level, :].any(axis=0) & blocking
        entry = {
            "no_context": covered(cov, base, blocking), "every_hunk": covered(cov, np.full(hunks, level), blocking),
            "every_hunk_gains": int((at_level & ~at_none).sum()), "every_hunk_loses": int((at_none & ~at_level).sum()),
            "only_the_must_fix_hunks": covered(cov, only_must, blocking),
            "random": {"hunks_up": float(np.mean([(choice > 0).mean() for choice in random])), "must_fix_up": float(random_must.mean()),
                       "must_fix_up_p95": float(np.percentile(random_must, 95)), "covered": float(random_covered.mean()),
                       "covered_p95": float(np.percentile(random_covered, 95))},
            "selectors": {},
        }
        for name in [*shown, "cheapest first"]:
            score = -(tok[:, level] - tok[:, 0]) if name == "cheapest first" else scores[name]
            up = upgrade_choice(tok, order_of(score), level, spend) > 0
            entry["selectors"][name] = {
                "hunks_up": float(up.mean()), "must_fix_up": [int(up[must].sum()), int(must.sum())], "other_up": float(up[~must].mean()),
                "must_fix_minus_other": [share_gap(up, must), *interval(repo, lambda i, u=up: share_gap(u, must, i), draws, seed)],
                "covered": covered(cov, np.where(up, level, 0), blocking),
            }
            entry["selectors"][name]["random_at_least"] = float((random_covered >= entry["selectors"][name]["covered"]).mean())
        report["budget"][data["levels"][level]] = entry
    return report


def _pct(value: float) -> str:
    return f"{100 * value:.1f}%"


def _points(triple) -> str:
    return f"{100 * triple[0]:+.1f} [{100 * triple[1]:+.1f}, {100 * triple[2]:+.1f}]"


def _auc(triple) -> str:
    return f"{triple[0]:.3f} [{triple[1]:.3f}, {triple[2]:.3f}]"


def print_report(report: dict) -> None:
    names = {**report["names"], "changed lines": "changed lines alone", "cheapest first": "cheapest hunks first"}
    print(f"{report['hunks']:,} hunks in {report['repositories']} repositories; {report['references']:,} reference comments, "
          f"{report['blocking_references']} blocking ({report['blocking_on_a_hunk']} on a reviewed hunk's lines)")
    print(f"hunks with a reference comment on their lines: {report['hunks_with_a_comment']}; with a blocking one (must-fix hunks): "
          f"{report['must_fix_hunks']}; with a blocking or should-fix one: {report['serious_hunks']}")
    print(f"hunks where a wider level covers a blocking comment that no context misses: {report['hunks_where_context_gains_a_blocking_comment']}")

    print("\n1. Does the score rank the must-fix hunks higher? AUC [95% interval]; 0.5 is a coin flip")
    print(f"   {'':34s}{'must-fix hunk':>24s}{'must-fix or should-fix':>26s}{'among commented hunks':>26s}{'in size fifths':>16s}")
    for name, row in report["ranking"].items():
        print(f"   {names[name]:34s}{_auc(row['must_fix']):>24s}{_auc(row['serious']):>26s}{_auc(row['must_fix_among_commented']):>26s}"
              f"{row['must_fix_within_size_fifths']:>16.3f}")

    print("\n2. A zero-shot model's own answer (Yes: give more code)")
    for name, row in report["answers"].items():
        print(f"   {names[name]}")
        for label, (yes, total) in row["yes"].items():
            print(f"      {label:34s} Yes for {yes:5d} of {total:5d} = {_pct(yes / total)}")
        other = row["other_hunks"]
        print(f"      {'hunks without a must-fix comment':34s} Yes for {other[0]:5d} of {other[1]:5d} = {_pct(other[0] / other[1])}")
        print(f"      must-fix hunks minus the other hunks, points: {_points(row['must_fix_minus_other'])}")

    print(f"\n3. At {report['spend']:.0%} of the extra tokens of an upgrade level")
    for level, entry in report["budget"].items():
        r = entry["random"]
        print(f"\n   to {SHORT[level]}: blocking comments covered of {report['blocking_references']}: no context for every hunk "
              f"{entry['no_context']}, every hunk upgraded {entry['every_hunk']} (gains {entry['every_hunk_gains']}, loses "
              f"{entry['every_hunk_loses']}), only the must-fix hunks upgraded {entry['only_the_must_fix_hunks']}")
        print(f"   random choice: {_pct(r['hunks_up'])} of hunks go up, {_pct(r['must_fix_up'])} of must-fix hunks "
              f"(95% of orders under {_pct(r['must_fix_up_p95'])}); blocking covered mean {r['covered']:.1f}, 95% of orders under {r['covered_p95']:.0f}")
        print(f"   {'':34s}{'hunks up':>10s}{'must-fix hunks up':>20s}{'other hunks up':>16s}{'difference, points':>24s}{'blocking covered':>18s}{'random orders as high':>24s}")
        for name, row in entry["selectors"].items():
            up, total = row["must_fix_up"]
            print(f"   {names[name]:34s}{_pct(row['hunks_up']):>10s}{f'{_pct(up / total)} ({up}/{total})':>20s}{_pct(row['other_up']):>16s}"
                  f"{_points(row['must_fix_minus_other']):>24s}{row['covered']:>18d}{_pct(row['random_at_least']):>24s}")


# ---------- figure ----------

def figure(report: dict, path: Path) -> None:
    """Left: the share of must-fix hunks and of the other hunks that each selector gives more
    context. Right: the blocking comments covered under each policy, per upgrade level."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from figure_style import AQUA, BLUE, DPI, INK, INK2, MUTED, ORANGE, SURFACE, style

    function = report["budget"]["H02_function"]
    spend = f"{report['spend']:.0%}"
    rows = []
    if "zero-shot 7B" in report["answers"]:
        answer = report["answers"]["zero-shot 7B"]
        yes, total = answer["yes"]["must-fix comment on the hunk"]
        rows.append(("7B model answers Yes", yes / total, answer["other_hunks"][0] / answer["other_hunks"][1]))
    for name, label in (("zero-shot 7B", f"7B model, {spend} budget"), ("tabular, logistic", f"hunk facts (logistic), {spend} budget"),
                        ("non-LLM: tabular, random forest", f"hunk facts (forest), {spend} budget")):
        if name in function["selectors"]:
            row = function["selectors"][name]
            rows.append((label, row["must_fix_up"][0] / row["must_fix_up"][1], row["other_up"]))
    rows.append((f"random choice, {spend} budget", function["random"]["must_fix_up"], function["random"]["hunks_up"]))

    fig, (left, right) = plt.subplots(1, 2, figsize=(12.4, 4.4), dpi=DPI, facecolor=SURFACE, gridspec_kw={"width_ratios": [1.15, 1]})
    fig.subplots_adjust(left=0.205, right=0.965, top=0.83, bottom=0.2, wspace=0.42)

    style(left, "x")
    for k, (label, must, other) in enumerate(rows):
        y = len(rows) - 1 - k
        left.plot([100 * other, 100 * must], [y, y], color=MUTED, linewidth=1.5, zorder=2)
        left.plot([100 * other], [y], marker="o", markersize=8, color=BLUE, markeredgecolor=SURFACE, markeredgewidth=1.5, zorder=3)
        left.plot([100 * must], [y], marker="o", markersize=8, color=ORANGE, markeredgecolor=SURFACE, markeredgewidth=1.5, zorder=3)
        low, high = sorted((other, must))
        if high - low < 0.015:                                    # the two dots sit on each other: one label
            left.annotate(f"{100 * high:.0f}% of both", (100 * high, y), xytext=(8, 0), textcoords="offset points", ha="left", va="center",
                          fontsize=8, color=INK2)
            continue
        left.annotate(f"{100 * low:.0f}%", (100 * low, y), xytext=(-8, 0), textcoords="offset points", ha="right", va="center", fontsize=8, color=INK2)
        left.annotate(f"{100 * high:.0f}%", (100 * high, y), xytext=(8, 0), textcoords="offset points", ha="left", va="center", fontsize=8, color=INK2)
    left.set_yticks(range(len(rows)), [row[0] for row in reversed(rows)])
    left.tick_params(axis="y", labelsize=9, labelcolor=INK2)
    left.set_xlim(0, 100)
    left.set_ylim(-0.6, len(rows) - 0.4)
    left.set_xlabel("share of the hunks given more context (%)", fontsize=8.5, color=INK2)
    left.set_title("Hunks given more context (upgrade to the function level)", fontsize=9.5, color=INK, loc="left", pad=8)
    left.legend(handles=[Line2D([], [], marker="o", linestyle="none", markersize=8, color=ORANGE, markeredgecolor=SURFACE,
                                label=f"the {report['must_fix_hunks']} hunks with a must-fix comment"),
                         Line2D([], [], marker="o", linestyle="none", markersize=8, color=BLUE, markeredgecolor=SURFACE, label="the other hunks")],
                loc="upper center", bbox_to_anchor=(0.5, -0.17), ncol=2, frameon=False, fontsize=8.5, labelcolor=INK2, handletextpad=0.3)

    style(right, "x")
    levels = list(report["budget"])
    series = [(f"random choice, {spend} budget (average)", MUTED, lambda e: e["random"]["covered"]),
              (f"7B model, {spend} budget", ORANGE, lambda e: e["selectors"]["zero-shot 7B"]["covered"] if "zero-shot 7B" in e["selectors"] else None),
              ("only the must-fix hunks upgraded", AQUA, lambda e: e["only_the_must_fix_hunks"]),
              ("every hunk upgraded", BLUE, lambda e: e["every_hunk"])]
    none = report["budget"][levels[0]]["no_context"]
    right.axvline(none, color=INK2, linewidth=1, linestyle=(0, (3, 2)), zorder=1)
    right.annotate(f"no context for every hunk: {none}", (none, len(levels) - 0.45), xytext=(-4, 0), textcoords="offset points",
                   ha="right", va="center", fontsize=8, color=INK2)
    for k, level in enumerate(levels):
        entry = report["budget"][level]
        for j, (_, color, value) in enumerate(series):
            if value(entry) is None:
                continue
            y = len(levels) - 1 - k + (1.5 - j) * 0.17
            right.plot([value(entry)], [y], marker="o", markersize=8, color=color, markeredgecolor=SURFACE, markeredgewidth=1.5, zorder=3)
            right.annotate(f"{value(entry):.0f}", (value(entry), y), xytext=(8, 0), textcoords="offset points", va="center", fontsize=8, color=INK2)
    right.set_yticks(range(len(levels)), [f"to the {SHORT[level]} level" for level in reversed(levels)])
    right.tick_params(axis="y", labelsize=9, labelcolor=INK2)
    right.set_xlim(none - 14, max(report["budget"][level]["every_hunk"] for level in levels) + 5)
    right.set_ylim(-0.6, len(levels) - 0.3)
    right.set_xlabel(f"must-fix (blocking) comments covered, of {report['blocking_references']}", fontsize=8.5, color=INK2)
    right.set_title("Must-fix comments covered under each policy", fontsize=9.5, color=INK, loc="left", pad=8)
    right.legend(handles=[Line2D([], [], marker="o", linestyle="none", markersize=8, color=color, markeredgecolor=SURFACE, label=label)
                          for label, color, _ in series], loc="upper center", bbox_to_anchor=(0.33, -0.17), ncol=2, frameon=False,
                 fontsize=8.5, labelcolor=INK2, handletextpad=0.3, columnspacing=1.0)
    fig.suptitle("The selectors give more context to must-fix hunks, but no more must-fix comments are covered", fontsize=11.5, color=INK,
                 x=0.02, ha="left", y=0.965)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=DPI, facecolor=SURFACE)
    plt.close(fig)


# ---------- main ----------

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--outcomes", type=Path, default=OUTCOMES, help="the selectors' input file, hunk_outcomes.json")
    parser.add_argument("--scores", type=Path, default=SCORES, help="the selectors' out-of-fold scores, llm_oof_scores.csv")
    parser.add_argument("--profile", default="cloud-hunk-kept", help="the per-hunk notebook profile of the run")
    parser.add_argument("--spend", type=float, default=0.3, help="the share of an upgrade level's extra tokens to spend (default 0.3)")
    parser.add_argument("--json", type=Path, help="where to write the report (default: must_fix.json next to the scores)")
    parser.add_argument("--figure", type=Path, default=FIGURE)
    parser.add_argument("--no-figure", action="store_true")
    args = parser.parse_args(argv)

    data = load_outcomes(args.outcomes)
    if data["levels"] != LEVELS:
        raise SystemExit(f"{args.outcomes} holds the levels {data['levels']}, not {LEVELS}")
    report = build_report(data, references_on_hunks(args.profile, data), load_scores(args.scores, data["hunks"]), args.spend)
    print_report(report)
    target = args.json or args.scores.with_name("must_fix.json")
    target.write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"\nwrote {target}")
    if not args.no_figure:
        figure(report, args.figure)
        print(f"drew {args.figure}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
