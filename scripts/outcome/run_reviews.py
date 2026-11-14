"""Run the outcome table's review stage from a terminal, with the same settings and code as
notebooks/outcome_table.ipynb, so a long run can sit in `screen` and survive a dropped connection.

It reads the profile from the notebook, reviews each (PR, level) cell once with the reviewer
server, and saves each reply as it arrives under <out_root>/<run_name>/reviews/. Re-running
resumes: saved cells are reused and failed calls are retried. Every line also goes to
<out_root>/<run_name>/logs/reviews_<time>.log. The judge stage is not run here.

    .venv/bin/python scripts/outcome/run_reviews.py --first 3   # the first 3 PRs, to time a run
    .venv/bin/python scripts/outcome/run_reviews.py             # every PR
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.review_outcomes import backends, table  # noqa: E402
from src.review_outcomes.parse import PARSER_VERSION  # noqa: E402
from src.review_outcomes.profiles import read_notebook_profile  # noqa: E402
from src.review_outcomes.prompts import (  # noqa: E402
    JUDGE_PROMPT_VERSION, REVIEW_PROMPT_VERSION, REVIEW_REQUEST, RUBRIC_SHA256, load_rubric, prompt_sha256,
)
from src.tracer_context.aacr import file_sha256  # noqa: E402

log = table.log


def make_reviewer(spec: dict):
    if spec["backend"] != "openai":
        raise SystemExit(f"this script runs profiles with an OpenAI-compatible reviewer, not {spec['backend']!r}")
    return backends.OpenAIChat(spec["base_url"], spec["model"], api_key=os.environ.get(spec.get("api_key_env", "")),
                               max_tokens=spec["max_tokens"], enable_thinking=spec.get("enable_thinking"),
                               model_revision=spec.get("model_revision"))


def select_cells(cells: list, first: int | None, entries: list[str] | None) -> list:
    """The cells of the chosen PRs: the first `first` in dataset order, or the named ones."""
    order = list(dict.fromkeys(cell.entry for cell in cells))
    if entries:
        unknown = sorted(set(entries) - set(order))
        if unknown:
            raise SystemExit(f"not in the context file: {', '.join(unknown)}")
        chosen = set(entries)
    else:
        chosen = set(order[:first] if first else order)
    return [cell for cell in cells if cell.entry in chosen]


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default=os.environ.get("OUTCOME_PROFILE", "cloud"), help="notebook profile (default cloud)")
    parser.add_argument("--first", type=int, help="only the first N PRs of the context file (to time a run)")
    parser.add_argument("--entries", nargs="+", metavar="ID", help="only these PRs")
    parser.add_argument("--concurrency", type=int, help="requests in flight (default: the profile's)")
    args = parser.parse_args(argv)

    config = read_notebook_profile(args.profile)
    spec = config["reviewer"]
    out = PROJECT_ROOT / config["out_root"] / config["run_name"]
    contexts, dataset = PROJECT_ROOT / config["contexts"], PROJECT_ROOT / "data" / "aacr" / "positive_samples.json"
    concurrency = args.concurrency or spec["concurrency"]
    log_path = table.start_run_log(out, console_level=logging.INFO)
    rubric = load_rubric()
    reviewer = make_reviewer(spec)
    commit = subprocess.run(["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    log.info(f"profile {args.profile}, run folder {out}, code commit {commit[:7]}, server {spec['base_url']}")

    all_cells = table.discover_cells(contexts, config["entries"], config["levels"])
    cells = select_cells(all_cells, args.first, args.entries)
    limit = spec["max_input_tokens"]
    if len(cells) != len(all_cells):
        log.info(f"limited to {len({c.entry for c in cells})} of {len({c.entry for c in all_cells})} PRs: "
                 f"{', '.join(dict.fromkeys(c.entry for c in cells))}")
    table.write_json(out / "manifest.json", {
        "profile": args.profile, "config": config, "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code_commit": commit, "rubric_sha256": RUBRIC_SHA256, "review_request": REVIEW_REQUEST,
        "prompt_sha256": prompt_sha256(rubric), "review_prompt_version": REVIEW_PROMPT_VERSION,
        "judge_prompt_version": JUDGE_PROMPT_VERSION, "parser_version": PARSER_VERSION,
        "reviewer_identity": reviewer.identity, "dataset": str(dataset.relative_to(PROJECT_ROOT)),
        "dataset_sha256": file_sha256(dataset), "contexts": str(contexts.relative_to(PROJECT_ROOT)),
        "contexts_sha256": file_sha256(contexts), "concurrency": concurrency,
    })

    started = time.monotonic()
    reviews = table.run_reviews(cells, reviewer, rubric, out, max_input_tokens=limit, concurrency=concurrency)
    seconds = time.monotonic() - started

    sent = [r for r in reviews if r.get("finished_at") and not r.get("reused_from") and r.get("reply")]
    sent_tokens = sum(r["input_tokens"] or 0 for r in sent)
    if sent and seconds > 0 and len(cells) != len(all_cells):
        # What is left of the whole table, at this run's speed (input tokens dominate the time).
        done = {(r["entry"], r["level"]) for r in reviews}
        seen, left_calls, left_tokens = {table.sha256_text(c.text) for c in cells}, 0, 0
        for cell in all_cells:
            digest = table.sha256_text(cell.text)
            if (cell.entry, cell.level) in done or digest in seen or (cell.input_tokens or 0) > limit:
                continue
            seen.add(digest)
            left_calls, left_tokens = left_calls + 1, left_tokens + (cell.input_tokens or 0)
        rate = sent_tokens / seconds
        log.info(f"this run: {len(sent)} calls, {sent_tokens:,} input tokens, "
                 f"{sum(r['reply']['completion_tokens'] or 0 for r in sent):,} output tokens in {table._duration(seconds)} "
                 f"({rate:,.0f} input tokens/s). The rest of the table is {left_calls} calls and {left_tokens:,} input "
                 f"tokens: about {table._duration(left_tokens / rate)} at this speed")
    errors = sum(r["status"] == "error" for r in reviews)
    log.info(f"log file {log_path}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
