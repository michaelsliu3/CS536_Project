"""The severity-weighted review score (docs/review_score_design.md).

    importance(reference) = severity weight x category multiplier x evidence multiplier

    score = sum of importance over the covered references / sum of importance over all references

It is recall in which a reference comment counts by how much it matters. Everything is read
from files a run already has: `coverage.csv` (which references each level covered, from the
judge) and a label file (each reference's severity tier). No model is called.

The reviewer's findings that match no reference are not scored. Rating them as valid,
speculative or wrong was tried and dropped: the only rater available is the reviewer's own
model (Section 13 of the document).
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
    category_multiplier: dict = field(default_factory=dict)  # {category: multiplier}; missing = 1
    human_multiplier: float = 1.0                             # a reference written by a person

    def importance(self, tier: str, category: str | None = None, human: bool = False) -> float:
        return (self.weights[TIERS.index(tier)] * self.category_multiplier.get(category, 1.0)
                * (self.human_multiplier if human else 1.0))


def read_coverage(run_dir: Path) -> list[dict]:
    with (Path(run_dir) / "coverage.csv").open(newline="", encoding="utf-8") as handle:
        return [{**row, "reference": int(row["reference"]), "covered": row["covered"] == "True",
                 "human": row["is_ai_comment"] == "False"} for row in csv.DictReader(handle)]


def score_rows(coverage: list[dict], labels: dict, params: Params = Params()) -> list[dict]:
    """One row per (PR, level): references and covered ones per tier, the summed importance of
    each (`reference_weight`, `covered_weight`), and `score`, their ratio. A reference with no
    label is counted in `unlabelled` and left out of every sum."""
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
    for row in rows.values():
        row["score"] = round(row["covered_weight"] / row["reference_weight"], 4) if row["reference_weight"] else None
    return [row for _, row in sorted(rows.items())]


def summarize(rows: list[dict]) -> list[dict]:
    """One row per level, summed over the PRs: references covered per tier and the score."""
    levels: dict[str, list[dict]] = {}
    for row in rows:
        levels.setdefault(row["level"], []).append(row)
    out = []
    for level, group in levels.items():
        total = lambda name: sum(r[name] for r in group)  # noqa: E731
        summary = {"level": level, "prs": len(group), "unlabelled": total("unlabelled")}
        for tier in TIERS:
            summary[f"{tier}_covered"], summary[f"{tier}_references"] = total(f"{tier}_covered"), total(f"{tier}_references")
        weight = total("reference_weight")
        summary["score"] = round(total("covered_weight") / weight, 4) if weight else None
        out.append(summary)
    return out
