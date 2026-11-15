#!/usr/bin/env python3
"""Score a finished per-hunk run with the severity-weighted review score
(docs/review_score_design.md). Calls no model.

    .venv/bin/python scripts/outcome/score_runs.py data/aacr_hunk/outcomes/qwen3.8-27b_aacr-hunk_v2 \\
        --labels data/aacr_hunk/labels/severity_qwen3.8-27b.jsonl

Reads the run's coverage.csv (the judge's matches), the label file and, if the findings were
rated (rate_findings.py), the run's ratings. Writes score_table.csv to the run folder (one row
per PR and level, under the agreed weights) and prints one table per level: recall per tier,
then weighted recall under every weighting, then the score when ratings exist.
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.review_outcomes import score, severity  # noqa: E402
from src.review_outcomes.severity import TIERS  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("run_dir", type=Path, help="a run folder with coverage.csv")
    parser.add_argument("--labels", type=Path, required=True, help="the severity label file")
    parser.add_argument("--ratings", default="ratings", help="the run's folder of ratings (ratings, or ratings_think)")
    parser.add_argument("--no-write", action="store_true", help="print only")
    args = parser.parse_args(argv)

    coverage = score.read_coverage(args.run_dir)
    labels = severity.load_labels(args.labels)
    ratings = severity.load_ratings(args.run_dir, args.ratings) or None
    rows = score.score_rows(coverage, labels, ratings)
    summary = score.summarize(rows)
    print(f"run {args.run_dir}\nlabels {args.labels} ({len(labels)} labels); "
          + (f"{len(ratings)} rated findings" if ratings else "findings not rated: first term of the score only"))
    if summary and summary[0]["unlabelled"]:
        print(f"WARNING: {summary[0]['unlabelled']} references per level have no label and are left out")

    print("\nReferences covered per tier, and weighted recall under each weighting")
    names = list(score.WEIGHTINGS)
    print(f"{'level':17s} " + " ".join(f"{t:>11s}" for t in TIERS) + "  " + " ".join(f"{n:>13s}" for n in names))
    by_weighting = {name: {s["level"]: s for s in score.summarize(score.score_rows(
        coverage, labels, ratings, score.Params(weights=weights)))} for name, weights in score.WEIGHTINGS.items()}
    for level in summary:
        tiers = " ".join(f"{level[f'{t}_covered']:>4d} of {level[f'{t}_references']:<3d}" for t in TIERS)
        recalls = " ".join(f"{by_weighting[n][level['level']]['weighted_recall']:>13.3f}" for n in names)
        print(f"{level['level']:17s} {tiers}  {recalls}")
    if ratings:
        print("\nWith rated findings: valid findings outside the references, noise, and the score divided by "
              "the references' summed importance")
        print(f"{'level':17s} {'valid':>6s} {'noise':>6s}  " + " ".join(f"{n:>13s}" for n in names))
        for level in summary:
            scores = " ".join(f"{by_weighting[n][level['level']]['score_norm']:>13.3f}" for n in names)
            print(f"{level['level']:17s} {level['valid_outside']:>6d} {level['noise']:>6d}  {scores}")
    if not args.no_write and rows:
        path = args.run_dir / ("score_table.csv" if args.ratings == "ratings" else f"score_table_{args.ratings}.csv")
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nwrote {path} ({len(rows)} rows, agreed weights)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
