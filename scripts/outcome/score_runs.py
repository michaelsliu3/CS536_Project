#!/usr/bin/env python3
"""Score a finished run with the severity-weighted review score
(docs/review_score_design.md). Calls no model.

    .venv/bin/python scripts/outcome/score_runs.py data/aacr_hunk/outcomes/qwen3.8-27b_aacr-hunk_v2 \\
        --labels data/aacr_hunk/labels/severity_qwen3.8-27b_think.jsonl

Reads the run's coverage.csv (the judge's matches) and the label file. Writes score_table.csv
to the run folder (one row per PR and level, under the agreed weights) and prints one row per
level: references covered per tier, then the score under every weighting.
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
    parser.add_argument("--no-write", action="store_true", help="print only")
    args = parser.parse_args(argv)

    coverage = score.read_coverage(args.run_dir)
    labels = severity.load_labels(args.labels)
    rows = score.score_rows(coverage, labels)
    summary = score.summarize(rows)
    print(f"run {args.run_dir}\nlabels {args.labels} ({len(labels)} labels)")
    if summary and summary[0]["unlabelled"]:
        print(f"WARNING: {summary[0]['unlabelled']} references per level have no label and are left out")

    names = list(score.WEIGHTINGS)
    by_weighting = {name: {s["level"]: s for s in score.summarize(score.score_rows(
        coverage, labels, score.Params(weights=weights)))} for name, weights in score.WEIGHTINGS.items()}
    print("\nReferences covered per tier, and the score under each weighting")
    print(f"{'level':17s} " + " ".join(f"{t:>11s}" for t in TIERS) + "  " + " ".join(f"{n:>13s}" for n in names))
    for level in summary:
        tiers = " ".join(f"{level[f'{t}_covered']:>4d} of {level[f'{t}_references']:<3d}" for t in TIERS)
        scores = " ".join(f"{by_weighting[n][level['level']]['score']:>13.3f}" for n in names)
        print(f"{level['level']:17s} {tiers}  {scores}")
    if not args.no_write and rows:
        path = args.run_dir / "score_table.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        print(f"\nwrote {path} ({len(rows)} rows, agreed weights)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
