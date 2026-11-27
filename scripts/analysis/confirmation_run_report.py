#!/usr/bin/env python3
"""The numbers and figures of the write-up of the confirmation run (docs/aacr_confirmation_run.md):
the four kept levels on the AACR pull requests reviewed after the first 93, next to the first 93
and to all of them together. Calls no model.

    .venv/bin/python scripts/analysis/confirmation_run_report.py
    .venv/bin/python scripts/analysis/confirmation_run_report.py --no-figures

It reads:

- the run folder (--run): coverage.csv, table.csv, hunk_table.csv and the saved reviews;
- the severity labels (--labels);
- a frozen copy of the first run (--first-root, default data/aacr_hunk_50%): its coverage.csv and
  its labels as they were when the two comparisons were fixed. The pull requests in it are the
  first set, every other pull request of the run folder is the new set;
- the coverage.csv of runs that reviewed the same pull requests more than once (--rerun-runs), to
  set the levels' disagreement next to a level's disagreement with itself.

It prints the tables, writes confirmation_report.json into the run folder and draws two figures
into docs/figures/aacr_confirmation_run/.

Every interval is the middle 95% of 10,000 bootstrap draws of repositories (seed 536): a draw takes
whole repositories with replacement, from the repositories that hold a comment of the group.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from collections import defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from figure_style import AQUA, BLUE, DPI, INK, INK2, MUTED, ORANGE, SURFACE, style  # noqa: E402
from level_run_report import NAMES, PRICES, level_totals, read_csv  # noqa: E402
from selector_checks import repository  # noqa: E402
from src.review_outcomes import score, severity  # noqa: E402

LEVELS = ["H01_min", "H02_function", "H03_file", "H04_callees1"]
NONE, FUNCTION, FILE, CALLEES = LEVELS
TIERS = ("blocking", "should_fix", "nit")
CONTEXTS = ("Diff Level", "File Level", "Repo Level")
SETS = ("first", "new", "all")
SET_NAMES = {"first": "First 93 pull requests", "new": "New 92 pull requests", "all": "All 185 pull requests"}
# What a finding says, matched on its text (case ignored). The same patterns as in the offline
# check of November 26, written before the new pull requests were reviewed.
WORDING = {
    "asks the author to check": r"\b(?:ensure|verify|confirm|make sure|double[- ]check|please check|check) (?:that|whether|if)\b"
                                r"|\bplease (?:verify|confirm|ensure)\b|\bworth (?:verifying|checking|confirming)\b",
    "says code cannot be seen": r"not (?:shown|visible|defined|included|present|provided|declared)(?: \w+){0,3} (?:in|within) "
                                r"(?:the|this) (?:diff|hunk|visible|provided|snippet|shown)|visible (?:portion|scope|part|code)"
                                r"|(?:in|within) the (?:visible|provided|shown) (?:portion|scope|code|diff|snippet)"
                                r"|cannot (?:see|verify|determine|tell)|can't (?:see|verify|tell)|without seeing|not shown"
                                r"|outside (?:of )?(?:the|this) (?:diff|hunk)|is not visible|isn't visible"
                                r"|not (?:visible|shown) here|from (?:the|this) diff alone|based on the diff alone",
    "points to the given context": r"\b(?:in|from|per|within) the (?:provided |surrounding |given |relevant |code )?context\b"
                                   r"|visible in the context|elsewhere in (?:the|this) (?:file|class|module|codebase)"
                                   r"|rest of the (?:file|class|function|method|codebase)"
                                   r"|\bother (?:callers|usages|call sites|places|methods in)\b|as (?:seen|shown|defined|used) in\b"
                                   r"|consistent with (?:the )?(?:other|existing|rest)"
                                   r"|existing (?:pattern|convention|code|implementation)",
}


# ---------- reading ----------

def load_references(run: Path, labels: Path, keep=lambda entry: True) -> list[dict]:
    """One record per reference comment of the PRs `keep` accepts: its PR, repository, AACR context
    label and category, whether a person wrote it, its severity tier (None without a label) and
    whether each level covers it."""
    tiers = {key: record.get("tier") for key, record in severity.load_labels(labels).items()}
    references: dict[tuple[str, int], dict] = {}
    for row in read_csv(Path(run) / "coverage.csv"):
        if not keep(row["entry"]):
            continue
        key = (row["entry"], int(row["reference"]))
        record = references.setdefault(key, {
            "pr": row["entry"], "reference": key[1], "repository": repository(row["entry"]), "context": row["context"],
            "category": row["category"], "human": row["is_ai_comment"] == "False", "tier": tiers.get(key) or None, "covered": {}})
        record["covered"][row["level"]] = row["covered"] == "True"
    return list(references.values())


def read_findings(run: Path, keep=lambda entry: True) -> tuple[dict[str, dict], dict[str, dict], list[str]]:
    """For every hunk of the PRs `keep` accepts that has a review at each level: the text of its
    counted findings per level, and the tokens of each review as the model server counted them.
    Also the hunks left out because a level has no review."""
    hunks = sorted({(r["entry"], r["pr"]) for r in read_csv(Path(run) / "hunk_table.csv") if keep(r["pr"])})
    notes, tokens, left_out = {}, {}, []
    for hunk, _ in hunks:
        reviews = [json.loads((Path(run) / "reviews" / hunk / f"{level}.json").read_text(encoding="utf-8")) for level in LEVELS]
        if any(review.get("status") != "ok" for review in reviews):
            left_out.append(hunk)
            continue
        notes[hunk] = {level: [f.get("note") or "" for f in review["parsed"]["findings"]] for level, review in zip(LEVELS, reviews)}
        tokens[hunk] = {level: (review["reply"]["prompt_tokens"], review["reply"]["completion_tokens"])
                        for level, review in zip(LEVELS, reviews)}
    return notes, tokens, left_out


def hunk_outcomes(run: Path, keep=lambda entry: True) -> dict:
    """Over the hunks of the PRs `keep` accepts that have a review at each level: on how many each
    wider level covers more or fewer reference comments than no context, how many are positive
    (some wider level covers more), and the tokens of each level's text."""
    hunks: dict[str, dict] = defaultdict(dict)
    for row in read_csv(Path(run) / "hunk_table.csv"):
        if keep(row["pr"]):
            hunks[row["entry"]][row["level"]] = row
    reviewed = {hunk: cells for hunk, cells in hunks.items() if all(cells[level]["review_status"] == "ok" for level in LEVELS)}
    covered = {hunk: [int(cells[level]["references_covered"]) for level in LEVELS] for hunk, cells in reviewed.items()}
    prs = len({cells[NONE]["pr"] for cells in reviewed.values()})
    positives = sum(max(c[1:]) > c[0] for c in covered.values())
    return {"hunks": len(reviewed), "prs": prs, "nothing_at_any_level": sum(max(c) == 0 for c in covered.values()),
            "positives": positives, "positives_per_pr": positives / prs, "positives_per_hunk": positives / len(reviewed),
            "more": {level: sum(c[j] > c[0] for c in covered.values()) for j, level in enumerate(LEVELS) if j},
            "fewer": {level: sum(c[j] < c[0] for c in covered.values()) for j, level in enumerate(LEVELS) if j},
            "level_text_tokens": {level: sum(int(cells[level]["input_tokens"]) for cells in reviewed.values()) for level in LEVELS}}


# ---------- statistics ----------

def bootstrap(members: list[dict], change, draws: int = 10000, seed: int = 536) -> tuple[float, float, float] | None:
    """The mean of `change(reference)` over the members, with the middle 95% of bootstrap draws of
    the repositories that hold a member. None when there is no member."""
    import numpy as np
    if not members:
        return None
    names = sorted({m["repository"] for m in members})
    index = {name: i for i, name in enumerate(names)}
    total, count = np.zeros(len(names)), np.zeros(len(names))
    for m in members:
        total[index[m["repository"]]] += change(m)
        count[index[m["repository"]]] += 1
    picks = np.random.default_rng(seed).integers(0, len(names), size=(draws, len(names)))
    values = total[picks].sum(1) / count[picks].sum(1)
    return float(total.sum() / count.sum()), float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5))


def between(a: str, b: str):
    """The change in whether a reference is covered, from level `a` to level `b`."""
    return lambda m: float(m["covered"][b]) - float(m["covered"][a])


def wider_minus_none(m: dict) -> float:
    """Mean coverage at the three wider levels minus coverage with no context."""
    return sum(float(m["covered"][level]) for level in LEVELS[1:]) / 3 - float(m["covered"][NONE])


def exact_p(gained: int, lost: int) -> float:
    """Two-sided exact p that gains and losses are equally likely (the exact McNemar test)."""
    n, k = gained + lost, min(gained, lost)
    return 1.0 if n == 0 else min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def gained_lost(members: list[dict], a: str, b: str) -> dict:
    gained = sum(m["covered"][b] and not m["covered"][a] for m in members)
    lost = sum(m["covered"][a] and not m["covered"][b] for m in members)
    return {"gained": gained, "lost": lost, "p": exact_p(gained, lost)}


def covered_counts(members: list[dict]) -> list[int]:
    return [sum(m["covered"][level] for m in members) for level in LEVELS]


def severity_scores(members: list[dict], weights: tuple[float, float, float], draws: int = 10000, seed: int = 536) -> dict:
    """The severity-weighted score at each level over the labelled members, and each wider level's
    difference from no context with the middle 95% of bootstrap draws of repositories."""
    import numpy as np
    weight = dict(zip(TIERS, weights))
    labelled = [m for m in members if m["tier"] in weight]
    names = sorted({m["repository"] for m in labelled})
    index = {name: i for i, name in enumerate(names)}
    covered, total = np.zeros((len(names), len(LEVELS))), np.zeros(len(names))
    for m in labelled:
        w = weight[m["tier"]]
        covered[index[m["repository"]]] += [w * m["covered"][level] for level in LEVELS]
        total[index[m["repository"]]] += w
    picks = np.random.default_rng(seed).integers(0, len(names), size=(draws, len(names)))
    drawn_total = total[picks].sum(1)
    ok = drawn_total > 0
    drawn = covered[picks].sum(1)[ok] / drawn_total[ok][:, None]
    scores = covered.sum(0) / total.sum()
    gain = {level: (float(scores[j] - scores[0]), float(np.percentile(drawn[:, j] - drawn[:, 0], 2.5)),
                    float(np.percentile(drawn[:, j] - drawn[:, 0], 97.5))) for j, level in enumerate(LEVELS) if j}
    return {"score": [float(s) for s in scores], "gain": gain}


def wording_shares(notes: dict[str, dict], draws: int = 5000, seed: int = 536) -> dict:
    """Per kind of wording: the share of findings with it at each level, and each wider level's
    change from no context with the middle 95% of bootstrap draws of repositories."""
    import numpy as np
    hunks = sorted(notes)
    names = sorted({repository(h.rsplit("__h", 1)[0]) for h in hunks})
    where = np.array([names.index(repository(h.rsplit("__h", 1)[0])) for h in hunks])
    picks = np.random.default_rng(seed).integers(0, len(names), size=(draws, len(names)))

    def per_repository(values: list[float]):
        out = np.zeros(len(names))
        np.add.at(out, where, np.array(values, dtype=float))
        return out

    findings = {level: per_repository([len(notes[h][level]) for h in hunks]) for level in LEVELS}
    out = {"hunks": len(hunks), "repositories": len(names), "findings": {level: int(findings[level].sum()) for level in LEVELS}}
    for name, pattern in WORDING.items():
        regex = re.compile(pattern, re.I)
        hits = {level: per_repository([sum(bool(regex.search(note)) for note in notes[h][level]) for h in hunks]) for level in LEVELS}
        share = {level: float(hits[level].sum() / findings[level].sum()) for level in LEVELS}
        change = {}
        for level in LEVELS[1:]:
            with np.errstate(invalid="ignore", divide="ignore"):   # a draw with no finding at a level is left out
                values = (hits[level][picks].sum(1) / findings[level][picks].sum(1)
                          - hits[NONE][picks].sum(1) / findings[NONE][picks].sum(1))
            values = values[np.isfinite(values)]
            change[level] = (share[level] - share[NONE], float(np.percentile(values, 2.5)), float(np.percentile(values, 97.5)))
        out[name] = {"share": [share[level] for level in LEVELS], "change": change}
    return out


def token_totals(tokens: dict[str, dict], prs: int) -> dict:
    """Per level over the reviewed hunks: tokens as the model server counted them and what they
    would cost on a paid API, in total and per PR."""
    out = {}
    for level in LEVELS:
        prompt, output = sum(t[level][0] for t in tokens.values()), sum(t[level][1] for t in tokens.values())
        cost = (prompt * PRICES["input"] + output * PRICES["output"]) / 1_000_000
        out[level] = {"requests": len(tokens), "prompt_tokens": prompt, "output_tokens": output, "cost_usd": cost,
                      "prompt_per_pr": prompt / prs, "output_per_pr": output / prs, "cost_per_pr": cost / prs}
    return out


def rerun_disagreement(runs: list[Path]) -> dict | None:
    """On the reference comments that every one of `runs` holds (the same PRs reviewed again):
    how often one level's coverage of a comment differs between two runs, and how often two
    levels' coverage differs within a run. None unless at least two of the run folders exist."""
    runs = [run for run in runs if (run / "coverage.csv").is_file()]
    if len(runs) < 2:
        return None
    covered = [{(row["entry"], int(row["reference"]), row["level"]): row["covered"] == "True" for row in read_csv(run / "coverage.csv")
                if row["level"] in LEVELS} for run in runs]
    keys = sorted(set.intersection(*({key[:2] for key in table} for table in covered)))
    run_pairs = [(a, b) for i, a in enumerate(covered) for b in covered[i + 1:]]
    level_pairs = [(a, b) for i, a in enumerate(LEVELS) for b in LEVELS[i + 1:]]
    same = [a[(*key, level)] != b[(*key, level)] for a, b in run_pairs for level in LEVELS for key in keys]
    different = [table[(*key, a)] != table[(*key, b)] for table in covered for a, b in level_pairs for key in keys]
    return {"runs": [run.name for run in runs], "prs": len({key[0] for key in keys}), "references": len(keys),
            "same_level_two_runs": (sum(same), len(same)), "two_levels_one_run": (sum(different), len(different))}


def reading(result: tuple[float, float, float]) -> str:
    """How a fixed comparison is read on the new set, by the rule written before the run."""
    point, low, _ = result
    if point <= 0:
        return "not confirmed"
    return "confirmed" if low > 0 else "same direction, not confirmed on its own"


def summarize(members: list[dict]) -> dict:
    """Everything the write-up reports about one set of reference comments."""
    groups = {"all": members,
              **{tier: [m for m in members if m["tier"] == tier] for tier in TIERS},
              "important": [m for m in members if m["tier"] in ("blocking", "should_fix")],
              **{context: [m for m in members if m["context"] == context] for context in CONTEXTS},
              "human": [m for m in members if m["human"]], "ai": [m for m in members if not m["human"]]}
    out = {"prs": len({m["pr"] for m in members}), "repositories": len({m["repository"] for m in members}),
           "references": len(members), "unlabelled": sum(m["tier"] is None for m in members), "groups": {}}
    for name, group in groups.items():
        out["groups"][name] = {
            "n": len(group), "covered": covered_counts(group), "wider_minus_none": bootstrap(group, wider_minus_none),
            "vs_none": {level: bootstrap(group, between(NONE, level)) for level in LEVELS[1:]},
            "file_minus_function": bootstrap(group, between(FUNCTION, FILE)),
            "none_to_file": gained_lost(group, NONE, FILE), "function_to_file": gained_lost(group, FUNCTION, FILE)}
    out["comparison_1"] = out["groups"]["blocking"]["wider_minus_none"]
    out["comparison_2"] = out["groups"]["Repo Level"]["file_minus_function"]
    out["scores"] = {name: severity_scores(members, weights) for name, weights in score.WEIGHTINGS.items()}
    sums = [sum(m["covered"][level] for level in LEVELS) for m in members]
    out["never"], out["always"] = sum(s == 0 for s in sums), sum(s == len(LEVELS) for s in sums)
    out["changes"] = len(members) - out["never"] - out["always"]
    pairs = [(a, b) for i, a in enumerate(LEVELS) for b in LEVELS[i + 1:]]
    out["disagree"] = {f"{a}|{b}": sum(m["covered"][a] != m["covered"][b] for m in members) / len(members) for a, b in pairs}
    return out


# ---------- printing ----------

def show(result) -> str:
    """A change in percentage points with its interval."""
    return "n/a" if result is None else f"{100 * result[0]:+.1f} [{100 * result[1]:+.1f}, {100 * result[2]:+.1f}]"


def print_report(report: dict) -> None:
    sets = report["sets"]
    for key in SETS:
        s = sets[key]
        print(f"{SET_NAMES[key]}: {s['prs']} PRs, {s['repositories']} repositories, {s['references']} reference comments, "
              f"{s['unlabelled']} without a severity label; hunks reviewed at all four levels {s['wording']['hunks']}, "
              f"left out {s['hunks_left_out']}")
    print("\nComparison 1, blocking comments: mean of function, file and callees minus no context (points)")
    for key in SETS:
        g = sets[key]["groups"]["blocking"]
        print(f"  {key}: {g['n']} comments, covered {g['covered']}, {show(g['wider_minus_none'])}; no context to file "
              f"{show(g['vs_none'][FILE])}, gained {g['none_to_file']['gained']}, lost {g['none_to_file']['lost']}, "
              f"p {g['none_to_file']['p']:.3f}")
    print(f"  reading on the new set: {reading(sets['new']['comparison_1'])}")
    print("Comparison 2, Repo Level comments: file minus function (points)")
    for key in SETS:
        g = sets[key]["groups"]["Repo Level"]
        print(f"  {key}: {g['n']} comments, covered {g['covered']}, {show(g['file_minus_function'])}; gained "
              f"{g['function_to_file']['gained']}, lost {g['function_to_file']['lost']}, p {g['function_to_file']['p']:.3f}; "
              f"file minus no context {show(g['vs_none'][FILE])}")
    print(f"  reading on the new set: {reading(sets['new']['comparison_2'])}")
    print("\nCovered per group at none, function, file, callees; wider minus none; file minus function (points)")
    for key in SETS:
        for name, g in sets[key]["groups"].items():
            print(f"  {key:5s} {name:11s} {g['n']:5d}: {g['covered']}  {show(g['wider_minus_none'])}  {show(g['file_minus_function'])}")
    print("\nRecall, precision, tokens as the server counted them and cost, per level")
    for key in SETS:
        for level in LEVELS:
            t, k = sets[key]["totals"][level], sets[key]["tokens"][level]
            print(f"  {key:5s} {level:13s} covered {t['covered']} of {t['expected']} ({t['recall']:.3f}), precision {t['precision']:.3f}, "
                  f"findings {t['generated']}; input {k['prompt_tokens']:,} ({k['prompt_per_pr']:,.0f} per PR), output "
                  f"{k['output_tokens']:,} ({k['output_per_pr']:,.0f} per PR), ${k['cost_usd']:.2f} (${k['cost_per_pr']:.3f} per PR)")
        g = sets[key]["groups"]["all"]
        print(f"  {key:5s} recall against no context: " + "; ".join(f"{NAMES[lv]} {show(g['vs_none'][lv])}" for lv in LEVELS[1:]))
    print("\nSeverity score per level, and each wider level against no context")
    for name in score.WEIGHTINGS:
        for key in SETS:
            s = sets[key]["scores"][name]
            print(f"  {name:13s} {key:5s} " + " ".join(f"{v:.3f}" for v in s["score"]) + " | "
                  + "; ".join(f"{NAMES[lv]} {g[0]:+.3f} [{g[1]:+.3f}, {g[2]:+.3f}]" for lv, g in s["gain"].items()))
    print("\nWording of the findings: share at each level, and each wider level against no context (points)")
    for key in SETS:
        w = sets[key]["wording"]
        print(f"  {key}: findings {list(w['findings'].values())}")
        for name in WORDING:
            print(f"    {name:28s} " + " ".join(f"{100 * v:.1f}%" for v in w[name]["share"]) + " | "
                  + "; ".join(f"{NAMES[lv]} {show(c)}" for lv, c in w[name]["change"].items()))
    print("\nComments never covered, always covered, covered at some levels only; two levels disagreeing")
    for key in SETS:
        s = sets[key]
        print(f"  {key}: never {s['never']}, always {s['always']}, changes {s['changes']} of {s['references']} "
              f"({s['changes'] / s['references']:.1%}); mean disagreement of two levels {sum(s['disagree'].values()) / len(s['disagree']):.1%}; "
              + ", ".join(f"{pair} {share:.1%}" for pair, share in s["disagree"].items()))
    print("\nHunks where a wider level covers more or fewer reference comments than no context")
    for key in SETS:
        h = sets[key]["hunks"]
        print(f"  {key}: {h['hunks']} hunks; no reference covered at any level on {h['nothing_at_any_level']}; positive (some wider level "
              f"covers more) {h['positives']}: {h['positives_per_pr']:.2f} per PR, {h['positives_per_hunk']:.1%} of hunks; "
              + "; ".join(f"{NAMES[lv]} more on {h['more'][lv]}, fewer on {h['fewer'][lv]}" for lv in LEVELS[1:])
              + "; level text tokens per PR " + ", ".join(f"{h['level_text_tokens'][lv] / h['prs']:,.0f}" for lv in LEVELS))
    if report.get("rerun"):
        r = report["rerun"]
        (same, same_n), (different, different_n) = r["same_level_two_runs"], r["two_levels_one_run"]
        print(f"\nPull requests reviewed more than once ({', '.join(r['runs'])}): {r['prs']} PRs, {r['references']} reference comments; "
              f"the same level in two runs disagrees on {same} of {same_n} ({same / same_n:.1%}); two levels in one run on "
              f"{different} of {different_n} ({different / different_n:.1%})")
    print(f"\nreferences without a severity label in the run: {report['unlabelled']}")
    print(f"hunks with a level that has no review: {report['hunks_left_out']}")


# ---------- figures ----------

def _minus(text: str) -> str:
    """A typographic minus sign, and a zero without a sign."""
    text = "0.0" if text in ("+0.0", "-0.0") else text.replace("[+0.0,", "[0.0,").replace("[-0.0,", "[0.0,")
    return text.replace("-", "\N{MINUS SIGN}")


def _line_panel(ax, series, limits, ylabel, title, note):
    """Lines over the four levels, one per set, with the first and last value written at the ends."""
    from matplotlib.patches import Patch
    style(ax, "y")
    for _, color, values in series:
        ax.plot(range(4), values, color=color, linewidth=2, marker="o", markersize=6, markeredgecolor=SURFACE, markeredgewidth=1.5, zorder=3)
    gap = (limits[1] - limits[0]) * 0.06
    for x, side in ((0, "left"), (3, "right")):
        order = sorted(range(len(series)), key=lambda i: series[i][2][x])
        places = [series[i][2][x] for i in order]
        for k in range(1, len(places)):                      # push close labels apart
            places[k] = max(places[k], places[k - 1] + gap)
        shift = (sum(series[i][2][x] for i in order) - sum(places)) / len(places)
        for k, i in enumerate(order):
            ax.annotate(f"{series[i][2][x]:.0f}%", (x, places[k] + shift), textcoords="offset points",
                        xytext=(-8 if side == "left" else 8, 0), ha="right" if side == "left" else "left", va="center",
                        fontsize=8, color=INK2)
    ax.set_xticks(range(4), ["no context", "function", "file", "callees"])
    ax.set_xlim(-0.75, 3.75)
    ax.set_ylim(*limits)
    ax.set_ylabel(ylabel, fontsize=8.5, color=INK2)
    ax.set_title(title, fontsize=9.5, color=INK, loc="left")
    ax.legend(handles=[Patch(facecolor=color, edgecolor="none", label=label) for label, color, _ in series], loc="upper left",
              bbox_to_anchor=(-0.02, -0.11), ncol=1, frameon=False, fontsize=8, labelcolor=INK2, handlelength=1.0, handleheight=1.0,
              borderaxespad=0)
    ax.text(-0.02, -0.18 - 0.075 * len(series), _minus(note), transform=ax.transAxes, fontsize=8, color=INK, va="top", linespacing=1.45)


def figure_comparisons(report: dict, path: Path):
    """The two fixed comparisons and the wording check: the first set next to the new one."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    sets = report["sets"]
    pairs = [("first", BLUE), ("new", ORANGE)]
    share = lambda group: [100 * c / group["n"] for c in group["covered"]]
    fig, axes = plt.subplots(1, 3, figsize=(13.2, 5.3), dpi=DPI, facecolor=SURFACE)
    blocking = {key: sets[key]["groups"]["blocking"] for key, _ in pairs}
    _line_panel(axes[0], [(f"{SET_NAMES[key]} ({blocking[key]['n']} comments)", color, share(blocking[key])) for key, color in pairs],
                (40, 90), "Must-fix comments covered (%)",
                "1. Must-fix (blocking) comments\nfixed comparison: wider levels minus no context",
                "In points, 95% interval:\n" + "\n".join(f"{SET_NAMES[key].split(' pull')[0].lower()}  {show(blocking[key]['wider_minus_none'])}"
                                                         for key, _ in pairs) + "\nNot confirmed: the gain does not repeat.")
    repo = {key: sets[key]["groups"]["Repo Level"] for key, _ in pairs}
    _line_panel(axes[1], [(f"{SET_NAMES[key]} ({repo[key]['n']} comments)", color, share(repo[key])) for key, color in pairs],
                (30, 80), "Repo Level comments covered (%)",
                "2. Comments that need the repository (AACR label)\nfixed comparison: file minus function",
                "In points, 95% interval:\n" + "\n".join(f"{SET_NAMES[key].split(' pull')[0].lower()}  {show(repo[key]['file_minus_function'])}"
                                                         for key, _ in pairs) + "\nSame direction, not confirmed on its own.")
    name = "asks the author to check"
    words = {key: sets[key]["wording"] for key, _ in pairs}
    _line_panel(axes[2], [(f"{SET_NAMES[key]} ({sum(words[key]['findings'].values()) // 4:,} findings per level)", color,
                           [100 * v for v in words[key][name]["share"]]) for key, color in pairs],
                (25, 65), "Findings that ask the author to check (%)",
                "3. What the reviewer writes\nno references or labels used; not a fixed comparison",
                "No context to callees, in points, 95% interval:\n"
                + "\n".join(f"{SET_NAMES[key].split(' pull')[0].lower()}  {show(words[key][name]['change'][CALLEES])}" for key, _ in pairs)
                + "\nRepeats.")
    fig.suptitle("Do the level differences found on the first 93 pull requests repeat on 92 new ones?", fontsize=11.5, color=INK,
                 x=0.012, ha="left", y=0.985)
    fig.subplots_adjust(left=0.045, right=0.985, top=0.84, bottom=0.37, wspace=0.3)
    fig.savefig(path, dpi=DPI, facecolor=SURFACE)
    plt.close(fig)


def figure_gains(report: dict, path: Path):
    """Each wider level against no context, with its interval: recall and the severity score, for
    the first set, the new set and all pull requests."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Patch

    sets, colors = report["sets"], {"first": BLUE, "new": ORANGE, "all": AQUA}
    panels = [("Reference comments covered, change from no context (points)",
               lambda key, level: tuple(100 * v for v in sets[key]["groups"]["all"]["vs_none"][level]), "{:+.1f}", (-7, 8)),
              ("Severity score (weights 5, 2, 0.5), change from no context",
               lambda key, level: sets[key]["scores"]["agreed"]["gain"][level], "{:+.3f}", (-0.08, 0.15))]
    fig, axes = plt.subplots(1, 2, figsize=(11.6, 4.3), dpi=DPI, facecolor=SURFACE, sharey=True)
    fig.subplots_adjust(left=0.085, right=0.985, top=0.86, bottom=0.2, wspace=0.08)
    for ax, (title, value, form, limits) in zip(axes, panels):
        style(ax, "x")
        ax.axvline(0, color=MUTED, linewidth=1, zorder=1)
        for row, level in enumerate(LEVELS[1:]):
            for k, key in enumerate(SETS):
                y = (2 - row) + (1 - k) * 0.24
                point, low, high = value(key, level)
                ax.plot([low, high], [y, y], color=colors[key], linewidth=2, solid_capstyle="round", zorder=2)
                ax.plot([point], [y], marker="o", markersize=7, color=colors[key], markeredgecolor=SURFACE, markeredgewidth=1.5, zorder=3)
                ax.annotate(_minus(form.format(point)), (high, y), xytext=(6, 0), textcoords="offset points", va="center",
                            fontsize=8, color=INK2)
        ax.set_xlim(*limits)
        ax.set_ylim(-0.55, 2.55)
        ax.set_yticks([2, 1, 0], [NAMES[level] for level in LEVELS[1:]])
        ax.tick_params(axis="y", labelsize=9, labelcolor=INK2)
        ax.set_title(title, fontsize=9.5, color=INK, loc="left", pad=8)
    fig.legend(handles=[Patch(facecolor=colors[key], edgecolor="none", label=SET_NAMES[key]) for key in SETS], loc="lower center", ncol=3,
               frameon=False, fontsize=8.5, labelcolor=INK2, handlelength=1.0, handleheight=1.0, bbox_to_anchor=(0.5, 0.045))
    fig.text(0.5, 0.012, "Dot: the change. Line: the middle 95% of 10,000 bootstrap draws of repositories.", ha="center", fontsize=8,
             color=MUTED)
    fig.savefig(path, dpi=DPI, facecolor=SURFACE)
    plt.close(fig)


# ---------- main ----------

def build_report(run: Path, labels: Path, first_root: Path, rerun_runs: tuple[str, ...] = ()) -> dict:
    first_run = first_root / "outcomes" / run.name
    first_labels = first_root / "labels" / labels.name
    first_prs = {row["entry"] for row in read_csv(first_run / "coverage.csv")}
    table = read_csv(run / "table.csv")
    members = {"first": load_references(first_run, first_labels), "new": load_references(run, labels, lambda e: e not in first_prs),
               "all": load_references(run, labels)}
    keep = {"first": lambda e: e in first_prs, "new": lambda e: e not in first_prs, "all": lambda e: True}
    report = {"run": run.name, "levels": LEVELS, "sets": {}}
    for key in SETS:
        summary = summarize(members[key])
        notes, tokens, left_out = read_findings(run, keep[key])
        summary["totals"] = level_totals(table, LEVELS, keep[key])
        summary["wording"] = wording_shares(notes)
        summary["tokens"] = token_totals(tokens, summary["prs"])
        summary["hunks_left_out"] = left_out
        summary["hunks"] = hunk_outcomes(run, keep[key])
        report["sets"][key] = summary
    report["rerun"] = rerun_disagreement([run.parent / name for name in rerun_runs])
    report["unlabelled"] = sorted([m["pr"], m["reference"]] for m in members["all"] if m["tier"] is None)
    report["hunks_left_out"] = report["sets"]["all"]["hunks_left_out"]
    report["readings"] = {"comparison_1": reading(report["sets"]["new"]["comparison_1"]),
                          "comparison_2": reading(report["sets"]["new"]["comparison_2"])}
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run", type=Path, default=PROJECT_ROOT / "data/aacr_hunk/outcomes/qwen3.8-27b_aacr-hunk_kept4",
                        help="the run folder with every reviewed pull request")
    parser.add_argument("--labels", type=Path, default=PROJECT_ROOT / "data/aacr_hunk/labels/severity_qwen3.8-27b_think.jsonl",
                        help="the severity label file")
    parser.add_argument("--first-root", type=Path, default=PROJECT_ROOT / "data/aacr_hunk_50%",
                        help="the frozen copy of the first run: outcomes/<run name>/coverage.csv and labels/<label file>")
    parser.add_argument("--rerun-runs", nargs="*", default=["qwen3.8-27b_aacr-hunk_v2", "qwen3.8-27b_aacr-hunk_v2_r2", "qwen3.8-27b_aacr-hunk_v2_r3"],
                        help="run folders next to --run that reviewed the same PRs more than once; skipped when missing")
    parser.add_argument("--out", type=Path, default=PROJECT_ROOT / "docs" / "figures" / "aacr_confirmation_run")
    parser.add_argument("--no-figures", action="store_true", help="print the numbers only")
    args = parser.parse_args(argv)

    report = build_report(args.run, args.labels, args.first_root, tuple(args.rerun_runs))
    print_report(report)
    (args.run / "confirmation_report.json").write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(f"wrote {args.run / 'confirmation_report.json'}")
    if not args.no_figures:
        args.out.mkdir(parents=True, exist_ok=True)
        figure_comparisons(report, args.out / "comparisons.png")
        figure_gains(report, args.out / "gain_over_no_context.png")
        print(f"figures in {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
