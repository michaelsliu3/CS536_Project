#!/usr/bin/env python3
"""The final report's figures, drawn at the page's size from saved results. Calls no model.

    .venv/bin/python paper/final/make_figures.py

Reads the confirmation run's report (data/aacr_hunk/outcomes/.../confirmation_report.json, written by
scripts/analysis/confirmation_run_report.py), the selectors' budget table and the loop's report
(results/selector185_llm/), and the saved reviews for the cost of each fixed level. Writes
paper/final/figures/levels.pdf, frontier.pdf and numbers.json (the derived numbers the text quotes).
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/analysis"))

import selector_loop as loop  # noqa: E402
from figure_style import AQUA, BLUE, INK, INK2, MUTED, ORANGE, style  # noqa: E402

OUT = Path(__file__).resolve().parent / "figures"
RUN = ROOT / "data/aacr_hunk/outcomes/qwen3.8-27b_aacr-hunk_kept4"
RESULTS = ROOT / "results/selector185_llm"
NAMES = {"H01_min": "no context", "H02_function": "function", "H03_file": "file", "H04_callees1": "callees"}
SMALL, TINY = 7.5, 6.5


def setup():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({"font.size": SMALL, "pdf.fonttype": 42, "font.family": "DejaVu Sans"})
    return plt


def levels_figure(report: dict, path: Path) -> None:
    """Each wider level against no context on the first 93, the new 92 and all 185 pull requests:
    reference comments covered (points of recall) and the severity score, with 95% intervals."""
    plt = setup()
    from matplotlib.lines import Line2D
    sets = [("first", "first 93 pull requests", BLUE), ("new", "new 92 pull requests", ORANGE), ("all", "all 185 pull requests", AQUA)]
    wider = list(NAMES)[1:]
    fig, axes = plt.subplots(1, 2, figsize=(5.5, 1.95), facecolor="white")
    fig.subplots_adjust(left=0.105, right=0.985, top=0.88, bottom=0.27, wspace=0.12)
    panels = [("recall, change from no context (points)", lambda s, lv: [100 * v for v in report["sets"][s]["groups"]["all"]["vs_none"][lv]], "{:+.1f}"),
              ("severity score, change from no context", lambda s, lv: report["sets"][s]["scores"]["agreed"]["gain"][lv], "{:+.3f}")]
    for ax, (title, value, form) in zip(axes, panels):
        style(ax, "x")
        ax.set_facecolor("white")
        for k, level in enumerate(wider):
            for j, (name, _, color) in enumerate(sets):
                y = len(wider) - 1 - k + (1 - j) * 0.26
                point, low, high = value(name, level)
                ax.plot([low, high], [y, y], color=color, linewidth=1.4, zorder=2, solid_capstyle="round")
                ax.plot([point], [y], marker="o", markersize=4.2, color=color, markeredgecolor="white", markeredgewidth=0.7, zorder=3)
                text = form.format(point).replace("-", "\u2212")
                if float(form.format(point)) == 0:
                    text = text.lstrip("+\u2212")
                ax.annotate(text, (high, y), xytext=(3, 0), textcoords="offset points", va="center", fontsize=TINY, color=INK2)
        ax.axvline(0, color=INK2, linewidth=0.8, zorder=1)
        ax.set_ylim(-0.55, len(wider) - 0.45)
        ax.set_xlabel(title, fontsize=SMALL, color=INK2)
        ax.tick_params(axis="x", labelsize=TINY)
        low, high = ax.get_xlim()
        ax.set_xlim(low, high + 0.14 * (high - low))
    axes[0].set_yticks(range(len(wider)), [NAMES[level] for level in reversed(wider)])
    axes[0].tick_params(axis="y", labelsize=SMALL, labelcolor=INK)
    axes[1].set_yticks([])
    fig.legend(handles=[Line2D([], [], color=color, linewidth=1.4, marker="o", markersize=4.2, markeredgecolor="white", label=label)
                        for _, label, color in sets], loc="lower center", ncol=3, frameon=False, fontsize=SMALL, labelcolor=INK2,
               bbox_to_anchor=(0.54, -0.02), handletextpad=0.5, columnspacing=1.6)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor="white")
    plt.close(fig)


def frontier_figure(points: dict, path: Path) -> None:
    """Reference comments covered against cost: the fixed levels, a selector and random choice at
    one budget, the priced loop and the same dollars without its gate, and the two hindsight policies."""
    plt = setup()
    from matplotlib.lines import Line2D
    fig, ax = plt.subplots(figsize=(5.5, 2.75), facecolor="white")
    fig.subplots_adjust(left=0.085, right=0.625, top=0.97, bottom=0.155)
    style(ax, "y")
    ax.set_facecolor("white")
    for r in points["loop"][1:]:
        ax.plot([r["x"], r["x"]], [r["random_mean"], r["random_p95"]], color=MUTED, linewidth=0.9, zorder=2)
        ax.plot([r["x"]], [r["random_mean"]], marker="o", markersize=3.6, color=MUTED, markeredgecolor="white", markeredgewidth=0.6, zorder=3)
        ax.plot([r["x"]], [r["largest"]], marker="D", markersize=4.2, markerfacecolor="none", markeredgecolor=INK2, markeredgewidth=0.9, zorder=6)
    ax.plot([r["x"] for r in points["loop"]], [r["covered"] for r in points["loop"]], color=BLUE, linewidth=1.5, marker="o", markersize=4.6,
            markeredgecolor="white", markeredgewidth=0.8, zorder=5)
    for r in points["loop"][1:]:
        ax.annotate(f"{r['share']:.0%}", (r["x"], r["covered"]), xytext=(-3, 4), textcoords="offset points", ha="right", va="bottom",
                    fontsize=TINY, color=BLUE)
    for level, (x, covered) in points["fixed"].items():
        ax.plot([x], [covered], marker="s", markersize=4.6, color=INK, markeredgecolor="white", markeredgewidth=0.7, zorder=4)
        ax.annotate(NAMES[level], (x, covered), xytext=(-3 if x == 1 else 0, -5), textcoords="offset points", ha="left" if x == 1 else "center",
                    va="top", fontsize=TINY, color=INK)
    b = points["budget"]
    ax.plot([b["x"]], [b["selector"]], marker="^", markersize=8.5, markerfacecolor="none", markeredgecolor=AQUA, markeredgewidth=1.1, zorder=7)
    for x, covered, label in ((b["x"], b["hindsight"], "perfect per-hunk choice"), (points["oracle"]["x"], points["oracle"]["covered"], "loop with a perfect gate")):
        ax.plot([x], [covered], marker="*", markersize=8.5, color=ORANGE, markeredgecolor="white", markeredgewidth=0.6, zorder=7)
        ax.annotate(f"{label}: {covered}", (x, covered), xytext=(6, 0), textcoords="offset points", ha="left", va="center", fontsize=TINY, color=INK2)
    ax.set_xlim(0.95, 2.26)
    ax.set_ylim(722, 922)
    ax.set_xlabel("cost, as a multiple of one review per hunk with no context", fontsize=SMALL, color=INK2)
    ax.set_ylabel("reference comments covered, of 1,419", fontsize=SMALL, color=INK2)
    ax.tick_params(labelsize=TINY)
    handles = [Line2D([], [], linestyle="none", marker="s", markersize=4.6, color=INK, label="one fixed level for every hunk"),
               Line2D([], [], linestyle="none", marker="^", markersize=7, markerfacecolor="none", markeredgecolor=AQUA, markeredgewidth=1.1,
                      label="7B selector, 30% budget"),
               Line2D([], [], color=BLUE, linewidth=1.5, marker="o", markersize=4.6, label="priced loop, trained gate\n(label: hunks reviewed again)"),
               Line2D([], [], color=MUTED, linewidth=0.9, marker="o", markersize=3.6, label="same dollars at random\n(average to 95th percentile)"),
               Line2D([], [], linestyle="none", marker="D", markersize=4.2, markerfacecolor="none", markeredgecolor=INK2, label="same dollars, largest hunks first"),
               Line2D([], [], linestyle="none", marker="*", markersize=8.5, color=ORANGE, label="hindsight (knows the outcome)")]
    fig.legend(handles=handles, loc="center left", bbox_to_anchor=(0.632, 0.56), frameon=False, fontsize=TINY, labelcolor=INK2, handletextpad=0.6,
               labelspacing=0.9)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, facecolor="white")
    plt.close(fig)


def main() -> int:
    confirmation = json.loads((RUN / "confirmation_report.json").read_text(encoding="utf-8"))
    saved = json.loads((RESULTS / "selector_loop.json").read_text(encoding="utf-8"))
    with (RESULTS / "llm_budget_table.csv").open(newline="", encoding="utf-8") as handle:
        budget = {row["method"]: row for row in csv.DictReader(handle) if row["upgrade_to"] == "H02_function" and row["spend"] == "0.3"}

    data = loop.load(loop.OUTCOMES, loop.RUN, loop.FOLDS)
    hunks = len(data["hunks"])
    base = loop.evaluate(data, np.zeros(hunks, dtype=int))
    fixed = {}
    for k, level in enumerate(data["levels"]):
        row = loop.evaluate(data, np.full(hunks, k), np.full(hunks, k))
        fixed[level] = (row["cost_per_pr"] / base["cost_per_pr"], row["covered"])

    rows = saved["rows"]
    plain = [r for r in rows if r["start"] == "no context" and (r["policy"] == "no loop" or "equal_cost" in r)]
    looped = [{"x": r["cost_per_pr"] / base["cost_per_pr"], "covered": r["covered"], "share": r["hunks_with_more"] / hunks,
               "random_mean": r.get("equal_cost", {}).get("random_covered_mean"), "random_p95": r.get("equal_cost", {}).get("random_covered_p95"),
               "largest": r.get("equal_cost", {}).get("largest_first_covered")} for r in plain]
    start = next(r for r in rows if r["start"] == "selector" and r["policy"] == "no loop")
    oracle = next(r for r in rows if "hindsight" in r["policy"])
    seven = budget["zero-shot 7B"]
    points = {"fixed": fixed, "loop": looped,
              "budget": {"x": start["cost_per_pr"] / base["cost_per_pr"], "selector": int(seven["covered"]), "random_mean": float(seven["covered_mean"]),
                         "random_p95": float(seven["covered_p95"]), "hindsight": int(budget["hindsight (upper bound)"]["covered"])},
              "oracle": {"x": oracle["cost_per_pr"] / base["cost_per_pr"], "covered": oracle["covered"]}}
    assert start["covered"] == int(seven["covered"]), "the loop's accounting and the notebooks' disagree on the selector's start"

    levels_figure(confirmation, OUT / "levels.pdf")
    frontier_figure(points, OUT / "frontier.pdf")
    (OUT / "numbers.json").write_text(json.dumps(points, indent=1), encoding="utf-8")
    print(json.dumps(points, indent=1))
    print(f"drew {OUT / 'levels.pdf'} and {OUT / 'frontier.pdf'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
