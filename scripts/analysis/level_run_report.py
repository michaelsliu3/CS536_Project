#!/usr/bin/env python3
"""The numbers and figures of the write-up of the AACR 50% run (docs/aacr_50pct_run.md), from
the saved run, its score table and the selector checks. Calls no model.

    .venv/bin/python scripts/analysis/level_run_report.py
    .venv/bin/python scripts/analysis/level_run_report.py --no-figures

It reads, for a per-hunk profile (default cloud-hunk-kept):

- the run folder's table.csv, its saved reviews (for the tokens as the model server counted them)
  and score_table.csv (written by scripts/outcome/score_runs.py);
- <data root>/selector_test/rows_<run>.json, learning_curve_<run>.json and text_check_<run>.json
  (written by scripts/analysis/selector_checks.py rows, curve and text);
- the PRs of an earlier run (--earlier-run), to show the PRs reviewed before and the new ones apart;
- the rows of a second profile (--pilot-profile, default the ContextCRBench pilot), for its yield.

It draws five figures into docs/figures/aacr_50pct_run/.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import selector_checks as checks  # noqa: E402
from figure_style import AQUA, AXIS, BLUE, DPI, GRID, INK, INK2, MUTED, ORANGE, SURFACE, legend, rounded, style  # noqa: E402
from src.review_outcomes.profiles import read_notebook_profile  # noqa: E402

NAMES = {"H01_min": "No context", "H02_function": "Function", "H03_file": "Whole file", "H04_callees1": "Callees"}
SUMS = ("expected", "covered", "generated", "matched_generated", "input_tokens", "output_tokens", "human_expected",
        "human_covered", "ai_expected", "ai_covered")
TIERS = ("blocking", "should_fix", "nit")
# Dollars per million tokens, the price list of scripts/harness/record_codex.py (docs/codex_first_trial.md,
# Section 4). The reviewer's prompts share too little to be cached, so all input is at the full price.
PRICES = {"input": 2.00, "output": 10.00}


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def level_totals(table: list[dict], levels: list[str], keep=lambda entry: True) -> dict[str, dict]:
    """Per level: the table's counts summed over the PRs `keep` accepts, with recall and precision."""
    out = {}
    for level in levels:
        rows = [r for r in table if r["level"] == level and keep(r["entry"])]
        total = {name: sum(int(float(r[name] or 0)) for r in rows) for name in SUMS}
        total.update(prs=len(rows), recall=total["covered"] / total["expected"],
                     precision=total["matched_generated"] / total["generated"] if total["generated"] else 0.0)
        out[level] = total
    return out


def level_scores(score_table: list[dict], levels: list[str]) -> dict[str, dict]:
    """Per level: the severity-weighted score and the references covered per tier."""
    out = {}
    for level in levels:
        rows = [r for r in score_table if r["level"] == level]
        tiers = {tier: (sum(int(r[f"{tier}_covered"]) for r in rows), sum(int(r[f"{tier}_references"]) for r in rows)) for tier in TIERS}
        out[level] = {"score": sum(float(r["covered_weight"]) for r in rows) / sum(float(r["reference_weight"]) for r in rows),
                      "unlabelled": sum(int(r["unlabelled"]) for r in rows), **tiers}
    return out


def paired_bootstrap(table: list[dict], score_table: list[dict], levels: list[str], draws: int = 10000, seed: int = 536) -> dict:
    """Each later level against the first, with PRs drawn again with replacement: the difference in
    pooled recall and in the severity-weighted score, with the middle 95% of the draws. The same
    PRs are in both levels of a draw, so the interval is for the difference, not for each level."""
    import numpy as np
    entries = sorted({r["entry"] for r in table})
    index = {entry: i for i, entry in enumerate(entries)}
    covered, weight = {lv: np.zeros(len(entries)) for lv in levels}, {lv: np.zeros(len(entries)) for lv in levels}
    expected, reference_weight = np.zeros(len(entries)), np.zeros(len(entries))
    for r in table:
        covered[r["level"]][index[r["entry"]]] = int(r["covered"])
        expected[index[r["entry"]]] = int(r["expected"])
    for r in score_table:
        weight[r["level"]][index[r["entry"]]] = float(r["covered_weight"])
        reference_weight[index[r["entry"]]] = float(r["reference_weight"])
    picks = np.random.default_rng(seed).integers(0, len(entries), size=(draws, len(entries)))
    out, first = {}, levels[0]
    for level in levels[1:]:
        recall = (covered[level][picks].sum(1) - covered[first][picks].sum(1)) / expected[picks].sum(1)
        score = (weight[level][picks].sum(1) - weight[first][picks].sum(1)) / reference_weight[picks].sum(1)
        out[level] = {name: (float(values.mean()), float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5)),
                             float((values <= 0).mean()))
                      for name, values in (("recall", recall), ("score", score))}
    return out


def server_tokens(run: Path, hunks: set[str], levels: list[str]) -> dict[str, dict]:
    """Per level, over the reviewed hunks: input and output tokens as the model server counted them
    (the input includes the prompt's own instructions), and what they would cost on a paid API."""
    out = {}
    for level in levels:
        replies = [json.loads((run / "reviews" / hunk / f"{level}.json").read_text(encoding="utf-8"))["reply"] for hunk in sorted(hunks)]
        prompt, output = sum(r["prompt_tokens"] for r in replies), sum(r["completion_tokens"] for r in replies)
        out[level] = {"requests": len(replies), "prompt_tokens": prompt, "output_tokens": output,
                      "cost_usd": (prompt * PRICES["input"] + output * PRICES["output"]) / 1_000_000}
    return out


def yield_of(rows: list[dict], levels: list[str]) -> dict:
    positives = sum(checks.labels(rows, levels)["wider"])
    prs = len({r["pr"] for r in rows})
    return {"hunks": len(rows), "prs": prs, "positives": positives, "per_pr": positives / prs, "per_hunk": positives / len(rows),
            "gains_losses": checks.gains_losses(rows, levels)}


# ---------- figures ----------

def _bars(ax, names: list[str], values: list[float], title: str, limit: float, show, color=BLUE):
    """Horizontal bars, first name on top, each with its value at the end."""
    ax.set_xlim(0, limit)
    ax.set_ylim(-0.6, len(names) - 0.4)
    ax.set_yticks(range(len(names)), names[::-1])
    style(ax, "x")
    ax.tick_params(axis="y", labelsize=9, labelcolor=INK2)
    ax.set_title(title, fontsize=9.5, color=INK, loc="left", pad=8)
    for index, value in enumerate(values[::-1]):
        rounded(ax, 0, index - 0.21, value, index + 0.21, color if isinstance(color, str) else color[::-1][index], "right")
        ax.annotate(show(value), (value, index), xytext=(5, 0), textcoords="offset points", va="center", fontsize=8.5, color=INK)


def figure_levels(totals: dict, scores: dict, tokens: dict, levels: list[str], path: Path):
    """Three panels on the four levels: recall, severity score, cost per PR."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 3, figsize=(9.2, 2.6), dpi=DPI, sharey=True, facecolor=SURFACE)
    fig.subplots_adjust(left=0.105, right=0.965, top=0.83, bottom=0.12, wspace=0.2)
    names = [NAMES.get(lv, lv) for lv in levels]
    references = totals[levels[0]]["expected"]
    _bars(axes[0], names, [100 * totals[lv]["recall"] for lv in levels], f"Reference comments covered (of {references})", 100,
          lambda v: f"{v:.1f}%")
    axes[0].xaxis.set_major_formatter(lambda value, _: f"{value:.0f}%")
    _bars(axes[1], names, [scores[lv]["score"] for lv in levels], "Severity-weighted score", 1.0, lambda v: f"{v:.3f}")
    per_pr = [tokens[lv]["cost_usd"] / totals[lv]["prs"] for lv in levels]
    _bars(axes[2], names, per_pr, "Cost per pull request on a paid API", max(per_pr) * 1.3, lambda v: f"${v:.3f}")
    axes[2].xaxis.set_major_formatter(lambda value, _: f"${value:.2f}")
    fig.savefig(path, dpi=DPI, facecolor=SURFACE)
    plt.close(fig)


def figure_two_sets(first: dict, second: dict, levels: list[str], labels: tuple[str, str], path: Path):
    """Recall per level for two sets of PRs, as paired bars."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9.2, 3.2), dpi=DPI, facecolor=SURFACE)
    fig.subplots_adjust(left=0.12, right=0.95, top=0.87, bottom=0.2)
    names = [NAMES.get(lv, lv) for lv in levels][::-1]
    ax.set_xlim(0, 100)
    ax.set_ylim(-0.6, len(names) - 0.4)
    ax.set_yticks(range(len(names)), names)
    ax.xaxis.set_major_formatter(lambda value, _: f"{value:.0f}%")
    style(ax, "x")
    ax.tick_params(axis="y", labelsize=9, labelcolor=INK2)
    ax.set_title("Reference comments covered, by when the pull requests were reviewed", fontsize=9.5, color=INK, loc="left", pad=8)
    for index, level in enumerate(levels[::-1]):
        for offset, totals, color in ((0.19, first, BLUE), (-0.19, second, ORANGE)):
            value = 100 * totals[level]["recall"]
            rounded(ax, 0, index + offset - 0.16, value, index + offset + 0.16, color, "right")
            ax.annotate(f"{value:.1f}%  ({totals[level]['covered']} of {totals[level]['expected']})", (value, index + offset),
                        xytext=(5, 0), textcoords="offset points", va="center", fontsize=8.5, color=INK)
    legend(fig, [(labels[0], BLUE), (labels[1], ORANGE)])
    fig.savefig(path, dpi=DPI, facecolor=SURFACE)
    plt.close(fig)


def figure_gains_losses(result: dict, levels: list[str], hunks: int, path: Path):
    """Per wider level: hunks that cover more references than the first level (right) and fewer (left)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(9.2, 2.7), dpi=DPI, facecolor=SURFACE)
    fig.subplots_adjust(left=0.12, right=0.95, top=0.84, bottom=0.26)
    wider = levels[1:][::-1]
    reach = max(max(pair) for pair in result.values()) * 1.25
    ax.set_xlim(-reach, reach)
    ax.set_ylim(-0.6, len(wider) - 0.4)
    ax.set_yticks(range(len(wider)), [NAMES.get(lv, lv) for lv in wider])
    ax.xaxis.set_major_formatter(lambda value, _: f"{abs(value):.0f}")
    style(ax, "x")
    ax.spines["left"].set_visible(False)  # the baseline is the zero line in the middle
    ax.axvline(0, color=AXIS, linewidth=0.8, zorder=2)
    ax.tick_params(axis="y", labelsize=9, labelcolor=INK2)
    ax.set_title(f"Hunks where the level covers more or fewer reference comments than no context (of {hunks} hunks)",
                 fontsize=9.5, color=INK, loc="left", pad=8)
    for index, level in enumerate(wider):
        more, fewer = result[level]
        rounded(ax, 0, index - 0.21, more, index + 0.21, BLUE, "right")
        rounded(ax, 0, index - 0.21, -fewer, index + 0.21, ORANGE, "left")
        ax.annotate(f"{more}", (more, index), xytext=(5, 0), textcoords="offset points", va="center", fontsize=8.5, color=INK)
        ax.annotate(f"{fewer}", (-fewer, index), xytext=(-5, 0), textcoords="offset points", va="center", ha="right", fontsize=8.5,
                    color=INK)
    legend(fig, [("Covers fewer than no context", ORANGE), ("Covers more than no context", BLUE)], y=0.01)
    fig.savefig(path, dpi=DPI, facecolor=SURFACE)
    plt.close(fig)


def figure_curve(curve: dict, path: Path):
    """The ranking score against the training positives, one line per model, over the chance band."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    result = curve["labels"]["wider"]
    colors = {"logistic regression": BLUE, "random forest": ORANGE, "boosted trees": AQUA}
    fig, ax = plt.subplots(figsize=(9.2, 3.5), dpi=DPI, facecolor=SURFACE)
    fig.subplots_adjust(left=0.08, right=0.96, top=0.87, bottom=0.27)
    first = next(iter(result["curve"].values()))
    xs = [point["train_positives"] for point in first.values()]
    chance = sum(v["auc_mean"] for v in result["shuffled"].values()) / len(result["shuffled"])
    spread = max(v["auc_sd"] for v in result["shuffled"].values())
    ax.set_xlim(0, max(xs) * 1.08)
    ax.set_ylim(0.40, 0.70)
    style(ax, "y")
    ax.axhspan(chance - 2 * spread, chance + 2 * spread, color=GRID, alpha=0.7, zorder=1, linewidth=0)
    ax.axhline(0.5, color=MUTED, linewidth=0.8, linestyle=(0, (4, 3)), zorder=2)
    for name, points in result["curve"].items():
        ys = [point["auc_mean"] for point in points.values()]
        ax.plot(xs, ys, color=colors[name], linewidth=2, marker="o", markersize=6.5, markeredgecolor=SURFACE, markeredgewidth=1.5,
                zorder=4, clip_on=False)
    ax.set_xticks(xs, [f"{x:.0f}\n{int(float(f) * 100)}% of the data" for x, f in zip(xs, first)])
    ax.tick_params(axis="x", labelsize=8.5, labelcolor=INK2)
    ax.set_xlabel("Positive hunks in training", fontsize=8.5, color=INK2, labelpad=6)
    ax.set_title(f"Ranking score of the selector as training data grows ({curve['hunks']} hunks, {curve['prs']} pull requests; "
                 "0.50 is chance, 1.00 is perfect)", fontsize=9.5, color=INK, loc="left", pad=8)
    handles = [Line2D([], [], color=color, linewidth=2, marker="o", markersize=6, markeredgecolor=SURFACE, label=name.capitalize())
               for name, color in colors.items()]
    handles.append(Patch(facecolor=GRID, edgecolor="none", label="Shuffled labels: chance, plus or minus two spreads"))
    fig.legend(handles=handles, loc="lower center", ncol=4, frameon=False, fontsize=8, labelcolor=INK2, bbox_to_anchor=(0.5, 0.0))
    fig.savefig(path, dpi=DPI, facecolor=SURFACE)
    plt.close(fig)


def figure_yield(ours: dict, pilot: dict, labels: tuple[str, str], path: Path):
    """Positive hunks per PR and per 100 reviewed hunks, for two data sets."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(9.2, 2.0), dpi=DPI, sharey=True, facecolor=SURFACE)
    fig.subplots_adjust(left=0.18, right=0.95, top=0.78, bottom=0.14, wspace=0.16)
    names, colors = list(labels), [BLUE, ORANGE]
    _bars(axes[0], names, [ours["per_pr"], pilot["per_pr"]], "Positive hunks per pull request", ours["per_pr"] * 1.3,
          lambda v: f"{v:.2f}", colors)
    _bars(axes[1], names, [100 * ours["per_hunk"], 100 * pilot["per_hunk"]], "Positive hunks per 100 reviewed hunks",
          100 * ours["per_hunk"] * 1.3, lambda v: f"{v:.1f}", colors)
    fig.savefig(path, dpi=DPI, facecolor=SURFACE)
    plt.close(fig)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default="cloud-hunk-kept", help="the per-hunk notebook profile of the run")
    parser.add_argument("--earlier-run", default="qwen3.8-27b_aacr-hunk_v2", help="a run whose PRs were reviewed before this one")
    parser.add_argument("--pilot-profile", default="cloud-ccrb-hunk", help="a second profile, for its yield of positive hunks")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "docs" / "figures" / "aacr_50pct_run")
    parser.add_argument("--no-figures", action="store_true", help="print the numbers only")
    args = parser.parse_args(argv)

    config = read_notebook_profile(args.profile)
    levels, paths = config["levels"], checks.run_paths(config)
    table = read_csv(paths["run"] / "table.csv")
    totals = level_totals(table, levels)
    score_table = read_csv(paths["run"] / "score_table.csv")
    scores = level_scores(score_table, levels)
    print(f"run {paths['run'].name}: {totals[levels[0]]['prs']} PRs, {totals[levels[0]]['expected']} references")
    print("level, covered, recall, precision, score, blocking, should fix, nit, human, AI, input tokens per PR, input, output")
    for level in levels:
        t, s = totals[level], scores[level]
        print(f"  {level}: {t['covered']}, {t['recall']:.3f}, {t['precision']:.3f}, {s['score']:.3f}, "
              + ", ".join(f"{s[tier][0]} of {s[tier][1]}" for tier in TIERS)
              + f", {t['human_covered']} of {t['human_expected']}, {t['ai_covered']} of {t['ai_expected']}, "
              f"{t['input_tokens'] / t['prs']:,.0f}, {t['input_tokens']:,}, {t['output_tokens']:,}")
    print(f"  references without a severity label, left out of the score: {scores[levels[0]]['unlabelled']}")
    print(f"each level against {levels[0]}, PRs drawn again 10,000 times: difference, middle 95%, share of draws at or below 0")
    for level, result in paired_bootstrap(table, score_table, levels).items():
        print(f"  {level}: " + "; ".join(f"{name} {mean:+.3f} [{low:+.3f}, {high:+.3f}], {below:.1%}"
                                         for name, (mean, low, high, below) in result.items()))
    hunks = {r["entry"] for r in read_csv(paths["run"] / "hunk_table.csv")}
    tokens = server_tokens(paths["run"], hunks, levels)
    print("level, requests, input tokens as the server counted them, output tokens, cost, cost per PR")
    for level in levels:
        t, prs = tokens[level], totals[level]["prs"]
        print(f"  {level}: {t['requests']}, {t['prompt_tokens']:,} ({t['prompt_tokens'] / prs:,.0f} per PR), {t['output_tokens']:,} "
              f"({t['output_tokens'] / prs:,.0f} per PR), ${t['cost_usd']:.2f}, ${t['cost_usd'] / prs:.3f}, "
              f"{t['cost_usd'] / tokens[levels[0]]['cost_usd']:.2f} times {levels[0]}")

    earlier = {r["entry"] for r in read_csv(PROJECT_ROOT / config["out_root"] / args.earlier_run / "table.csv")}
    before, new = level_totals(table, levels, lambda e: e in earlier), level_totals(table, levels, lambda e: e not in earlier)
    for name, part in ((f"reviewed before ({args.earlier_run})", before), ("new in this run", new)):
        print(f"{name}: {part[levels[0]]['prs']} PRs; "
              + "; ".join(f"{lv} {part[lv]['covered']} of {part[lv]['expected']} ({part[lv]['recall']:.3f})" for lv in levels))

    rows = json.loads(paths["rows"].read_text(encoding="utf-8"))["rows"]
    ours = yield_of(rows, levels)
    best, best_tokens = checks.union_covered(rows, lambda r: checks.hindsight_level(r, levels))
    base_tokens = checks.union_covered(rows, lambda r: levels[0])[1]
    print(f"hunks {ours['hunks']}; positive (a wider level covers more) {ours['positives']}: {ours['per_pr']:.2f} per PR, "
          f"{ours['per_hunk']:.1%} of hunks; gains and losses {ours['gains_losses']}")
    print(f"with hindsight: covered {best} ({best / totals[levels[0]]['expected']:.3f}) at {best_tokens:,} input tokens, "
          f"{best_tokens / base_tokens:.2f} times {levels[0]}; picks {dict(Counter(checks.hindsight_level(r, levels) for r in rows))}")

    curve = json.loads(paths["curve"].read_text(encoding="utf-8"))
    for label, result in curve["labels"].items():
        print(f"learning curve, label {label!r} ({result['positives']} positives, {curve['repeats']} splits):")
        for name, points in result["curve"].items():
            print(f"  {name}: " + ", ".join(f"{p['train_positives']:.0f} positives {p['auc_mean']:.3f}±{p['auc_sd']:.3f}"
                                            for p in points.values())
                  + f" | shuffled {result['shuffled'][name]['auc_mean']:.3f}±{result['shuffled'][name]['auc_sd']:.3f}, "
                  f"97.5% at {result['shuffled'][name]['auc_high']:.3f}")
    text = json.loads(paths["text"].read_text(encoding="utf-8"))
    for label, result in text["labels"].items():
        print(f"text check, {label!r} ({result['positives']} of {text['hunks']}): "
              + ", ".join(f"{kind} {result[kind]['auc_mean']:.3f}±{result[kind]['auc_sd']:.3f} (shuffled "
                          f"{result[kind]['shuffled_mean']:.3f})" for kind in ("words", "characters")))

    pilot_config = read_notebook_profile(args.pilot_profile)
    pilot_paths, pilot_levels = checks.run_paths(pilot_config), pilot_config["levels"]
    pilot_totals = level_totals(read_csv(pilot_paths["run"] / "table.csv"), pilot_levels)
    pilot = yield_of(json.loads(pilot_paths["rows"].read_text(encoding="utf-8"))["rows"], pilot_levels)
    print(f"{args.pilot_profile}: {pilot['prs']} PRs, {pilot['hunks']} hunks; "
          + "; ".join(f"{lv} {pilot_totals[lv]['covered']} of {pilot_totals[lv]['expected']} ({pilot_totals[lv]['recall']:.3f}), "
                      f"{pilot_totals[lv]['input_tokens'] / pilot_totals[lv]['prs']:,.0f} tokens per PR" for lv in pilot_levels))
    print(f"  positive {pilot['positives']}: {pilot['per_pr']:.2f} per PR, {pilot['per_hunk']:.1%} of hunks; "
          f"gains and losses {pilot['gains_losses']}")

    if not args.no_figures:
        args.out.mkdir(parents=True, exist_ok=True)
        figure_levels(totals, scores, tokens, levels, args.out / "levels.png")
        figure_two_sets(before, new, levels, (f"The {before[levels[0]]['prs']} pull requests reviewed before",
                                              f"The {new[levels[0]]['prs']} new pull requests"), args.out / "earlier_and_new.png")
        figure_gains_losses(ours["gains_losses"], levels, ours["hunks"], args.out / "gains_and_losses.png")
        figure_curve(curve, args.out / "learning_curve.png")
        figure_yield(ours, pilot, (f"AACR-Bench\n{ours['prs']} pull requests", f"ContextCRBench pilot\n{pilot['prs']} pull requests"),
                     args.out / "yield.png")
        print(f"wrote {args.out}/levels.png, earlier_and_new.png, gains_and_losses.png, learning_curve.png, yield.png")
    return 0


if __name__ == "__main__":
    sys.exit(main())
