"""Run the outcome table's review stage from a terminal, with the same settings and code as
notebooks/outcome_table.ipynb, so a long run can sit in `screen` and survive a dropped connection.

It reads the profile from the notebook, reviews each (PR, level) cell once with the reviewer
server, and saves each reply as it arrives under <out_root>/<run_name>/reviews/. A profile whose
context file was built per diff hunk (`cloud-hunk`) has one cell per (hunk, level); `--first` and
`--entries` still choose PRs, and the profile's "prompt" entry names the reviewer prompt. Re-running
resumes: saved cells are reused and failed calls are retried. When the profile's reviewer has a
"retry" entry, a reply that was cut off or finished without the JSON gets the retry steps of
src/review_outcomes/retry.py, including replies saved by an earlier run. Every line also goes to
<out_root>/<run_name>/logs/reviews_<time>.log. The judge stage is not run here.

    .venv/bin/python scripts/outcome/run_reviews.py --first 3   # the first 3 PRs, to time a run
    .venv/bin/python scripts/outcome/run_reviews.py             # every PR
    .venv/bin/python scripts/outcome/run_reviews.py --dry-run   # what a run would send; no server needed
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
from src.review_outcomes.profiles import dataset_path, read_notebook_profile  # noqa: E402
from src.review_outcomes.retry import RetryPolicy  # noqa: E402
from src.review_outcomes.prompts import (  # noqa: E402
    JUDGE_PROMPT_VERSION, REVIEW_REQUEST, RUBRIC_SHA256, load_review_prompt,
)
from src.tracer_context.aacr import file_sha256  # noqa: E402

log = table.log


def make_reviewer(spec: dict):
    if spec["backend"] != "openai":
        raise SystemExit(f"this script runs profiles with an OpenAI-compatible reviewer, not {spec['backend']!r}")
    return backends.OpenAIChat(spec["base_url"], spec["model"], api_key=os.environ.get(spec.get("api_key_env", "")),
                               max_tokens=spec["max_tokens"], enable_thinking=spec.get("enable_thinking"),
                               model_revision=spec.get("model_revision"))


def describe_plan(plan: dict, retry: RetryPolicy | None, out: Path) -> str:
    """The dry run's report: what a run would do for these cells."""
    lines = [f"run folder {out}",
             f"{plan['cells']} cells from {plan['prs']} PRs, {plan['distinct']} distinct inputs"
             + (f" ({plan['hunks']} diff hunks, one review each per level)" if plan["hunks"] else ""),
             f"  already saved: {plan['saved']}",
             f"  too long to send: {plan['too_long']}",
             f"  to send for the first time: {plan['send']['cells']} ({plan['send']['input_tokens']:,} input tokens)"]
    if retry is None:
        return "\n".join(lines + ["no retry policy in this profile"])
    longer, follow = plan["retry"]["longer_output"], plan["retry"]["follow_up"]
    return "\n".join(lines + [
        f"saved replies due a retry step: {longer['cells'] + follow['cells']}",
        f"  cut off, to send again with an output limit of up to {retry.longer_max_tokens}: {longer['cells']} "
        f"({longer['input_tokens']:,} input tokens)",
        f"  finished without the JSON, to get the follow-up message: {follow['cells']} "
        f"({follow['input_tokens']:,} input tokens)",
        f"  due a step but with no room in the {retry.context_window}-token window: {plan['no_room']}",
        "a new cell that is cut off or finishes without the JSON gets the same steps; nothing was sent or written"])


def select_cells(cells: list, first: int | None, entries: list[str] | None) -> list:
    """The cells of the chosen PRs: the first `first` in dataset order, or the named ones. A
    per-hunk cell is chosen by its PR."""
    order = list(dict.fromkeys(cell.group for cell in cells))
    if entries:
        unknown = sorted(set(entries) - set(order))
        if unknown:
            raise SystemExit(f"not in the context file: {', '.join(unknown)}")
        chosen = set(entries)
    else:
        chosen = set(order[:first] if first else order)
    return [cell for cell in cells if cell.group in chosen]


def profile_cells(config: dict, contexts: Path, dataset: Path, levels) -> list:
    """The profile's cells: its PRs at `levels`, and with "only_reference_files" only the hunks in
    a file that has a reference comment (table.in_reference_files)."""
    cells = table.discover_cells(contexts, config["entries"], levels)
    return table.in_reference_files(cells, dataset) if config.get("only_reference_files") else cells


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default=os.environ.get("OUTCOME_PROFILE", "cloud"), help="notebook profile (default cloud)")
    parser.add_argument("--first", type=int, help="only the first N PRs of the context file (to time a run)")
    parser.add_argument("--entries", nargs="+", metavar="ID", help="only these PRs")
    parser.add_argument("--levels", nargs="+", metavar="LEVEL", help="only these levels (default: the profile's)")
    parser.add_argument("--run-suffix", default="", metavar="TEXT",
                        help="added to the profile's run name, e.g. _r2: a repeat of a run in a folder of its own")
    parser.add_argument("--concurrency", type=int, help="requests in flight (default: the profile's)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print what a run would send (new cells and retry steps) and stop; calls no model, writes nothing")
    args = parser.parse_args(argv)

    config = read_notebook_profile(args.profile)
    spec = config["reviewer"]
    out = PROJECT_ROOT / config["out_root"] / (config["run_name"] + args.run_suffix)
    contexts, dataset = PROJECT_ROOT / config["contexts"], dataset_path(config)
    concurrency = args.concurrency or spec["concurrency"]
    prompt = load_review_prompt(config.get("prompt", "codex"))  # refuses to run if a pinned prompt file was edited
    reviewer = make_reviewer(spec)
    retry = RetryPolicy.from_spec(spec, prompt.follow_up)
    if args.dry_run:
        cells = select_cells(profile_cells(config, contexts, dataset, args.levels or config["levels"]), args.first, args.entries)
        plan = table.plan_reviews(cells, reviewer, prompt, out, max_input_tokens=spec["max_input_tokens"], retry=retry)
        print(describe_plan(plan, retry, out))
        return 0
    log_path = table.start_run_log(out, console_level=logging.INFO)
    commit = subprocess.run(["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    log.info(f"profile {args.profile}, run folder {out}, code commit {commit[:7]}, server {spec['base_url']}")

    all_cells = profile_cells(config, contexts, dataset, args.levels or config["levels"])
    cells = select_cells(all_cells, args.first, args.entries)
    limit = spec["max_input_tokens"]
    if len(cells) != len(all_cells):
        log.info(f"limited to {len({c.group for c in cells})} of {len({c.group for c in all_cells})} PRs: "
                 f"{', '.join(dict.fromkeys(c.group for c in cells))}")
    table.write_json(out / "manifest.json", {
        "profile": args.profile, "config": config, "written_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "code_commit": commit, "prompt_name": prompt.name,
        **({"rubric_sha256": RUBRIC_SHA256, "review_request": REVIEW_REQUEST} if prompt.name == "codex" else {}),
        "prompt_sha256": prompt.sha256, "review_prompt_version": prompt.version,
        "judge_prompt_version": JUDGE_PROMPT_VERSION, "parser_version": PARSER_VERSION,
        "reviewer_identity": reviewer.identity, "dataset": str(dataset.relative_to(PROJECT_ROOT)),
        "dataset_sha256": file_sha256(dataset), "contexts": str(contexts.relative_to(PROJECT_ROOT)),
        "contexts_sha256": file_sha256(contexts), "concurrency": concurrency,
        "retry_policy": retry.identity if retry else None,
    })

    started, started_at = time.monotonic(), table._now()
    reviews = table.run_reviews(cells, reviewer, prompt, out, max_input_tokens=limit, concurrency=concurrency,
                                retry=retry)
    seconds = time.monotonic() - started

    # First calls made by this run; a saved review, and a saved one that only took a retry step, are not.
    sent = [r for r in reviews if (r.get("finished_at") or "") >= started_at and not r.get("reused_from") and r.get("reply")]
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
