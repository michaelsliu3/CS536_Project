#!/usr/bin/env python3
"""Rate the findings of a per-hunk run that the judge matched to no reference: valid,
speculative or wrong, with a severity tier (docs/review_score_design.md, Sections 4 and 6).

    OUTCOME_PROFILE=cloud-hunk .venv/bin/python scripts/outcome/rate_findings.py --dry-run
    OUTCOME_PROFILE=cloud-hunk .venv/bin/python scripts/outcome/rate_findings.py --levels H01_min H03_file

The rater is the profile's judge model. It sees the finding and the input the reviewer saw.
Ratings are saved to <run folder>/ratings/<entry>/<level>.json; a later run keeps the ratings
whose prompt and model settings are unchanged. Run the judge first (run_judgments.py).
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from label_severity import model_name, model_spec  # noqa: E402 - the sibling script
from run_judgments import load_reviews  # noqa: E402
from run_reviews import make_reviewer as make_chat, select_cells  # noqa: E402
from src.review_outcomes import severity, table  # noqa: E402
from src.review_outcomes.profiles import read_notebook_profile  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default=os.environ.get("OUTCOME_PROFILE", "cloud-hunk"), help="a per-hunk notebook profile")
    parser.add_argument("--first", type=int, help="only the first N PRs of the context file")
    parser.add_argument("--entries", nargs="+", metavar="ID", help="only these PRs")
    parser.add_argument("--levels", nargs="+", metavar="LEVEL", help="only these levels (default: the profile's)")
    parser.add_argument("--thinking", action="store_true", help="rate with the model's thinking on")
    parser.add_argument("--max-tokens", type=int, help="output limit (default 1024, or 16000 with --thinking)")
    parser.add_argument("--concurrency", type=int, help="calls at a time (default: the profile's)")
    parser.add_argument("--dry-run", action="store_true", help="print what would be rated and stop; calls no model")
    args = parser.parse_args(argv)

    config = read_notebook_profile(args.profile)
    spec = model_spec(config["judge"], args.thinking, args.max_tokens)
    run_dir = PROJECT_ROOT / config["out_root"] / config["run_name"]
    cells = select_cells(table.discover_cells(PROJECT_ROOT / config["contexts"], config["entries"],
                                              args.levels or config["levels"]), args.first, args.entries)
    reviews, missing = load_reviews(run_dir, cells)
    judgments = [j for j in (table.read_json(run_dir / "judgments" / r["entry"] / f"{r['level']}.json") for r in reviews) if j]
    chat = make_chat(spec)
    items = severity.finding_items(reviews, judgments, cells, chat.identity)
    saved = severity.load_ratings(run_dir)
    kept = sum(1 for i in items if saved.get((i["entry"], i["level"], i["order"]), {}).get("key") == i["key"]
               and "error" not in saved[(i["entry"], i["level"], i["order"])])
    distinct = len({i["key"] for i in items})
    print(f"run folder {run_dir}\nrater {chat.identity}\n{len(reviews)} reviews ({len(missing)} cells have none), "
          f"{len(judgments)} judged; {len(items)} unmatched findings, {distinct} distinct; {kept} already rated")
    if args.dry_run:
        print("nothing was sent or written")
        return 0

    count = Counter()

    def progress(result: dict) -> None:
        count[result.get("validity") or "error"] += 1
        if sum(count.values()) % 25 == 0:
            print(f"  rated {sum(count.values())}: {dict(count)}", flush=True)

    ratings = severity.rate_findings(reviews, judgments, cells, chat, run_dir, args.concurrency or spec.get("concurrency", 1),
                                     rater=model_name(spec, args.thinking), progress=progress)
    by_level: dict[str, Counter] = {}
    for (_, level, _), record in ratings.items():
        by_level.setdefault(level, Counter())[record.get("validity") or "error"] += 1
    for level, counts in by_level.items():
        print(f"{level}: {dict(counts)}")
    errors = sum(counts["error"] for counts in by_level.values())
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
