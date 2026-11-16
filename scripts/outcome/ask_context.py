#!/usr/bin/env python3
"""Ask the reviewer, after its saved review of each hunk, whether it needs the function and the
file around the change (src/review_outcomes/ask.py).

    OUTCOME_PROFILE=cloud-hunk .venv/bin/python scripts/outcome/ask_context.py --dry-run
    OUTCOME_PROFILE=cloud-hunk .venv/bin/python scripts/outcome/ask_context.py --variant plain
    OUTCOME_PROFILE=cloud-hunk .venv/bin/python scripts/outcome/ask_context.py --variant cost

The question follows the saved review of `--level` in the same conversation, so no review is
made again. Answers go to <run folder>/asks/<level>_<variant>.jsonl, one line per hunk, and a
later run keeps the answers whose conversation and model settings are unchanged.
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

from run_reviews import make_reviewer, select_cells  # noqa: E402 - the sibling script
from src.review_outcomes import ask, table  # noqa: E402
from src.review_outcomes.profiles import read_notebook_profile  # noqa: E402
from src.review_outcomes.prompts import load_review_prompt  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default=os.environ.get("OUTCOME_PROFILE", "cloud-hunk"), help="a per-hunk notebook profile")
    parser.add_argument("--level", default="H01_min", help="the level whose saved reviews are asked about (default H01_min)")
    parser.add_argument("--variant", choices=sorted(ask.VARIANTS), default="plain",
                        help="plain: the question alone; cost: also says that a request has a price")
    parser.add_argument("--first", type=int, help="only the first N PRs of the context file")
    parser.add_argument("--entries", nargs="+", metavar="ID", help="only these PRs")
    parser.add_argument("--run-suffix", default="", metavar="TEXT", help="added to the profile's run name, as in run_reviews.py")
    parser.add_argument("--max-tokens", type=int, default=300, help="output limit of the answer (default 300)")
    parser.add_argument("--concurrency", type=int, help="calls at a time (default: the profile's)")
    parser.add_argument("--dry-run", action="store_true", help="print what would be asked and stop; calls no model")
    args = parser.parse_args(argv)

    config = read_notebook_profile(args.profile)
    spec = {**config["reviewer"], "max_tokens": args.max_tokens}
    run_dir = PROJECT_ROOT / config["out_root"] / (config["run_name"] + args.run_suffix)
    cells = select_cells(table.discover_cells(PROJECT_ROOT / config["contexts"], config["entries"], [args.level]),
                         args.first, args.entries)
    if not cells or cells[0].pr is None:
        raise SystemExit(f"profile {args.profile!r} has no per-hunk cells at {args.level}")
    reviews = {c.entry: table.read_json(run_dir / "reviews" / c.entry / f"{c.level}.json") for c in cells}
    chat = make_reviewer(spec)
    items = ask.ask_items(cells, reviews, load_review_prompt(config.get("prompt", "codex")), chat.identity, args.variant)
    out = ask.asks_path(run_dir, args.level, args.variant)
    saved = ask.load_asks(out)
    kept = sum(1 for i in items if saved.get(i["entry"], {}).get("key") == i["key"] and "error" not in saved[i["entry"]])
    print(f"answers file {out}\nmodel {chat.identity}\n{len(cells)} hunks in {len({c.pr for c in cells})} PRs at {args.level}; "
          f"{len(cells) - len(items)} have no finished review; {kept} already answered, {len(items) - kept} to ask")
    if args.dry_run:
        print("nothing was sent or written")
        return 0

    def progress(result: dict) -> None:
        answer = result.get("error") or f"{'yes' if result['need_file'] else 'no'} ({result['likelihood']}) {result['missing'][:80]}"
        print(f"  {result['entry']}: {answer}", flush=True)

    asks = ask.run_asks(items, chat, out, args.concurrency or spec.get("concurrency", 1), progress)
    errors = sum(1 for a in asks.values() if "error" in a)
    counts = Counter("error" if "error" in a else "yes" if a["need_file"] else "no" for a in asks.values())
    print(f"answers: {dict(counts)}; likelihood {dict(sorted(Counter(a.get('likelihood') for a in asks.values() if 'error' not in a).items(), key=str))}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
