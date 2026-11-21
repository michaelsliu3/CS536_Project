#!/usr/bin/env python3
"""Label the reference comments of a per-hunk profile's PRs when they come without labels (human
review comments, as in the ContextCRBench pilot): whether each is a finding at all, and its
category, context level and severity (src/review_outcomes/reference_labels.py).

    OUTCOME_PROFILE=cloud-ccrb-hunk .venv/bin/python scripts/outcome/label_references.py --dry-run
    OUTCOME_PROFILE=cloud-ccrb-hunk .venv/bin/python scripts/outcome/label_references.py --thinking

The model sees the comment, the replies in its thread, and the code it is about (the hunk the
comment sits on, with that hunk's context at `--code-level`). Labels are saved to
<out_root>/../labels/reference_labels_<model>[_think].jsonl, one line per comment, and a later
run keeps the labels whose prompt and model settings are unchanged.
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
from run_reviews import make_reviewer as make_chat, select_cells  # noqa: E402 - the sibling script
from src.review_outcomes import reference_labels, severity, table  # noqa: E402
from src.review_outcomes.profiles import dataset_path, read_notebook_profile  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default=os.environ.get("OUTCOME_PROFILE", "cloud-ccrb-hunk"), help="a per-hunk notebook profile")
    parser.add_argument("--first", type=int, help="only the first N PRs of the context file")
    parser.add_argument("--entries", nargs="+", metavar="ID", help="only these PRs")
    parser.add_argument("--code-level", default="H03_file", help="the level whose text is shown as the code (default H03_file)")
    parser.add_argument("--thinking", action="store_true", help="label with the model's thinking on (saved to its own file)")
    parser.add_argument("--max-tokens", type=int, help="output limit (default 1024, or 16000 with --thinking)")
    parser.add_argument("--concurrency", type=int, help="calls at a time (default: the profile's)")
    parser.add_argument("--dry-run", action="store_true", help="print what would be labelled and stop; calls no model")
    args = parser.parse_args(argv)

    config = read_notebook_profile(args.profile)
    spec = model_spec(config["judge"], args.thinking, args.max_tokens)
    name = model_name(spec, args.thinking)
    out = (PROJECT_ROOT / config["out_root"] / ".." / "labels" / f"reference_labels_{name}.jsonl").resolve()
    cells = select_cells(table.discover_cells(PROJECT_ROOT / config["contexts"], config["entries"], [args.code_level]),
                         args.first, args.entries)
    if not cells or cells[0].pr is None:
        raise SystemExit(f"profile {args.profile!r} has no per-hunk cells at {args.code_level}")
    references = table.references_by_entry(dataset_path(config), cells)
    chat = make_chat(spec)
    items = reference_labels.reference_items(references, cells, chat.identity)
    saved = severity.load_labels(out)
    kept = sum(1 for i in items if saved.get((i["pr"], i["reference"]), {}).get("key") == i["key"]
               and "error" not in saved[(i["pr"], i["reference"])])
    print(f"label file {out}\nmodel {chat.identity}\n{len(items)} reference comments in {len({i['pr'] for i in items})} PRs; "
          f"{kept} already labelled, {len(items) - kept} to label")
    if args.dry_run:
        print("nothing was sent or written")
        return 0

    def progress(result: dict) -> None:
        answer = result.get("error") or f"{'valid' if result['valid'] else 'not valid'} | {result['kind']} | {result['tier']}"
        print(f"  {result['pr']} #{result['reference']}: {answer}", flush=True)

    labels = reference_labels.label_references(references, cells, chat, out, args.concurrency or spec.get("concurrency", 1),
                                               labeller=name, progress=progress)
    errors = sum(1 for label in labels.values() if "error" in label)
    done = [label for label in labels.values() if "error" not in label]
    print(f"valid: {dict(Counter('yes' if label['valid'] else 'no' for label in done))}; "
          f"category: {dict(Counter(str(label['category']) for label in done if label['valid']))}; "
          f"context: {dict(Counter(str(label['context']) for label in done if label['valid']))}; "
          f"tier: {dict(Counter(str(label['tier']) for label in done if label['valid']))}; {errors} unreadable or failed")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
