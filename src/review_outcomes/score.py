"""The severity-weighted review score (docs/review_score_design.md).

    importance(reference) = severity weight x category multiplier x evidence multiplier

    score = sum of importance x match grade          over the references
          + outside credit x sum of severity weight  over valid findings that match no reference
          - noise penalty x (speculative + wrong findings)

Everything is read from files a run already has: `coverage.csv` (which references each level
covered, from the judge), a label file (each reference's severity tier) and, when the findings
have been rated, the run's `ratings/`. Without ratings only the first term can be computed,
and the rows say so.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path

from src.review_outcomes.severity import TIERS

# Blocking, should fix, nit. Every comparison is reported under all of them, so that no
# conclusion rests on one choice of weights.
WEIGHTINGS = {
    "flat": (1.0, 1.0, 1.0),          # plain recall
    "linear": (3.0, 2.0, 1.0),
    "agreed": (5.0, 2.0, 0.5),        # the starting values agreed on November 15, 2026
    "steep": (25.0, 5.0, 1.0),
    "blocking_only": (1.0, 0.0, 0.0),
}


@dataclass(frozen=True)
class Params:
    weights: tuple[float, float, float] = WEIGHTINGS["agreed"]
    outside_credit: float = 0.5   # share of its weight a valid finding outside the references earns
    noise_penalty: float = 0.5    # cost of one speculative or wrong finding
    category_multiplier: dict = field(default_factory=dict)  # {category: multiplier}; missing = 1
    human_multiplier: float = 1.0                             # a reference written by a person

    def weight(self, tier: str) -> float:
        return self.weights[TIERS.index(tier)]

    def importance(self, tier: str, category: str | None = None, human: bool = False) -> float:
        return (self.weight(tier) * self.category_multiplier.get(category, 1.0)
                * (self.human_multiplier if human else 1.0))


def read_coverage(run_dir: Path) -> list[dict]:
    with (Path(run_dir) / "coverage.csv").open(newline="", encoding="utf-8") as handle:
        return [{**row, "reference": int(row["reference"]), "covered": row["covered"] == "True",
                 "human": row["is_ai_comment"] == "False"} for row in csv.DictReader(handle)]


def pr_of(entry: str) -> str:
    """The PR of a hunk id (`<PR id>__h0001`), or the entry itself."""
    head, sep, tail = entry.rpartition("__h")
    return head if sep and tail.isdigit() else entry


def score_rows(coverage: list[dict], labels: dict, ratings: dict | None = None,
               params: Params = Params()) -> list[dict]:
    """One row per (PR, level).

    Per tier: references and covered ones. `covered_weight` and `reference_weight` give the
    first term and what it is divided by. With `ratings` ({(entry, level, order): record}) the
    row also has the valid findings outside the references per tier, the noise count, `score`
    and `score_norm` (the score divided by the PR's summed importance); without them those are
    None. A reference with no label is counted in `unlabelled` and left out of every sum."""
    rows: dict[tuple[str, str], dict] = {}
    for item in coverage:
        row = rows.setdefault((item["entry"], item["level"]), {
            "entry": item["entry"], "level": item["level"], "unlabelled": 0,
            **{f"{t}_references": 0 for t in TIERS}, **{f"{t}_covered": 0 for t in TIERS},
            "reference_weight": 0.0, "covered_weight": 0.0})
        label = labels.get((item["entry"], item["reference"]))
        if label is None or label.get("tier") not in TIERS:
            row["unlabelled"] += 1
            continue
        tier = label["tier"]
        importance = params.importance(tier, item.get("category"), item["human"])
        row[f"{tier}_references"] += 1
        row["reference_weight"] += importance
        if item["covered"]:
            row[f"{tier}_covered"] += 1
            row["covered_weight"] += importance

    rated: dict[tuple[str, str], dict] = {}
    for (entry, level, _), record in (ratings or {}).items():
        tally = rated.setdefault((pr_of(entry), level), {**{f"valid_{t}": 0 for t in TIERS},
                                                         "speculative": 0, "wrong": 0, "unrated": 0})
        validity, tier = record.get("validity"), record.get("tier")
        if validity == "valid" and tier in TIERS:
            tally[f"valid_{tier}"] += 1
        elif validity in ("speculative", "wrong"):
            tally[validity] += 1
        else:
            tally["unrated"] += 1

    out = []
    for key, row in sorted(rows.items()):
        row["weighted_recall"] = round(row["covered_weight"] / row["reference_weight"], 4) if row["reference_weight"] else None
        tally = rated.get(key) if ratings is not None else None
        if ratings is None:
            row.update({f"valid_{t}": None for t in TIERS}, speculative=None, wrong=None, unrated=None,
                       outside_weight=None, noise=None, score=None, score_norm=None)
        else:
            tally = tally or {**{f"valid_{t}": 0 for t in TIERS}, "speculative": 0, "wrong": 0, "unrated": 0}
            outside = sum(params.weight(t) * tally[f"valid_{t}"] for t in TIERS)
            noise = tally["speculative"] + tally["wrong"]
            score = row["covered_weight"] + params.outside_credit * outside - params.noise_penalty * noise
            row.update(tally, outside_weight=outside, noise=noise, score=round(score, 4),
                       score_norm=round(score / row["reference_weight"], 4) if row["reference_weight"] else None)
        out.append(row)
    return out


def summarize(rows: list[dict]) -> list[dict]:
    """One row per level, summed over the PRs: recall per tier, weighted recall, and (when the
    findings were rated) valid findings outside the references, noise and the score."""
    levels: dict[str, list[dict]] = {}
    for row in rows:
        levels.setdefault(row["level"], []).append(row)
    out = []
    for level, group in levels.items():
        total = lambda name: sum(r[name] or 0 for r in group)  # noqa: E731
        summary = {"level": level, "prs": len(group), "unlabelled": total("unlabelled")}
        for tier in TIERS:
            summary[f"{tier}_covered"], summary[f"{tier}_references"] = total(f"{tier}_covered"), total(f"{tier}_references")
        weight = total("reference_weight")
        summary["weighted_recall"] = round(total("covered_weight") / weight, 4) if weight else None
        if group[0]["score"] is None:
            summary.update(valid_outside=None, noise=None, score=None, score_norm=None)
        else:
            summary.update(valid_outside=sum(total(f"valid_{t}") for t in TIERS), noise=total("noise"),
                           score=round(total("score"), 2),
                           score_norm=round(total("score") / weight, 4) if weight else None)
        out.append(summary)
    return out
