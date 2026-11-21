"""Run the outcome table's judge stage from a terminal and write the table, with the same settings
and code as notebooks/outcome_table.ipynb.

It reads the profile from the notebook, loads the reviews saved by scripts/outcome/run_reviews.py,
scores each one with the profile's judge (AACR-Bench's file-level metric, one judge call per
generated comment), and writes judgments/, table.csv, table.json and coverage.csv under
<out_root>/<run_name>/. For a per-hunk profile (`cloud-hunk`) each hunk's findings are judged
against its PR's references, table.csv has one row per (PR, level) with the PR's hunk reviews put
together, and hunk_table.csv has one row per (hunk, level). It makes no reviewer call. Re-running resumes: saved judgments are reused
and failed judge calls are retried. Every line also goes to <run folder>/logs/judgments_<time>.log.

    .venv/bin/python scripts/outcome/run_judgments.py --dry-run   # what a run would judge; no server needed
    .venv/bin/python scripts/outcome/run_judgments.py --first 3   # the first 3 PRs
    .venv/bin/python scripts/outcome/run_judgments.py             # every PR, then the table
"""
from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
for folder in (PROJECT_ROOT, Path(__file__).resolve().parent):
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))

from run_reviews import make_reviewer as make_chat, profile_cells, select_cells  # noqa: E402 - the sibling script
from src.review_outcomes import backends, table  # noqa: E402
from src.review_outcomes.judge import normalize_separators  # noqa: E402
from src.review_outcomes.profiles import dataset_path, read_notebook_profile  # noqa: E402
from src.review_outcomes.prompts import JUDGE_PROMPT_VERSION  # noqa: E402

log = table.log


def load_reviews(out: Path, cells: list) -> tuple[list[dict], list[str]]:
    """The saved review of each cell, in cell order, and the cells that have none."""
    reviews, missing = [], []
    for cell in cells:
        record = table.read_json(out / "reviews" / cell.entry / f"{cell.level}.json")
        if record is None:
            missing.append(f"{cell.entry} {cell.level}")
        else:
            reviews.append(record)
    return reviews, missing


def describe(reviews: list[dict], missing: list[str], references: dict, out: Path, judge_identity) -> str:
    """What the judge stage has to work with: reviews by status, and the judge calls they need."""
    statuses: dict[str, int] = {}
    for review in reviews:
        statuses[review["status"]] = statuses.get(review["status"], 0) + 1
    distinct = {r["key"]: r for r in reviews if r["status"] in ("ok", "malformed", "empty_output")}
    findings = calls = 0
    for review in distinct.values():
        files = {normalize_separators(c.get("path")) for c in references[review["entry"]]["comments"] if c.get("note")}
        for finding in (review.get("parsed") or {}).get("findings", []):
            findings += 1
            calls += normalize_separators(finding.get("path")) in files  # no reference on the file: no call
    return "\n".join([
        f"run folder {out}",
        f"judge {judge_identity}",
        f"{len(reviews)} saved reviews, statuses {dict(sorted(statuses.items()))}; {len(missing)} cells have no saved review",
        f"{len(distinct)} distinct reviews to score, with {findings} findings; about {calls} judge calls "
        f"(a finding on a file without reference comments is counted as a miss without a call)"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default=os.environ.get("OUTCOME_PROFILE", "cloud"), help="notebook profile (default cloud)")
    parser.add_argument("--first", type=int, help="only the first N PRs of the context file")
    parser.add_argument("--entries", nargs="+", metavar="ID", help="only these PRs")
    parser.add_argument("--levels", nargs="+", metavar="LEVEL", help="only these levels (default: the profile's)")
    parser.add_argument("--run-suffix", default="", metavar="TEXT",
                        help="added to the profile's run name, e.g. _r2: a repeat of a run in a folder of its own")
    parser.add_argument("--concurrency", type=int, help="reviews judged at a time (default: the profile's)")
    parser.add_argument("--dry-run", action="store_true", help="print what a run would judge and stop; calls no model, writes nothing")
    args = parser.parse_args(argv)

    config = read_notebook_profile(args.profile)
    spec = config["judge"]
    if spec["backend"] != "openai" or "TO_BE_DECIDED" in spec.get("model", ""):
        raise SystemExit(f"profile {args.profile!r} has no judge served by an OpenAI-compatible server: {spec}")
    out = PROJECT_ROOT / config["out_root"] / (config["run_name"] + args.run_suffix)
    contexts, dataset = PROJECT_ROOT / config["contexts"], dataset_path(config)
    judge = backends.judge_caller(make_chat(spec))
    concurrency = args.concurrency or spec.get("concurrency", 1)
    all_cells = profile_cells(config, contexts, dataset, args.levels or config["levels"])
    cells = select_cells(all_cells, args.first, args.entries)
    reviews, missing = load_reviews(out, cells)
    references = table.references_by_entry(dataset, cells)
    summary = describe(reviews, missing, references, out, judge.identity)
    if args.dry_run:
        print(summary + "\nnothing was sent or written")
        return 0

    log_path = table.start_run_log(out, "judgments", console_level=logging.INFO)
    commit = subprocess.run(["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    log.info(f"profile {args.profile}, code commit {commit[:7]}, server {spec['base_url']}")
    for line in summary.splitlines():
        log.info(line)
    if missing:
        log.warning(f"no saved review for {len(missing)} cells (run scripts/outcome/run_reviews.py first): "
                    f"{', '.join(missing[:10])}{' ...' if len(missing) > 10 else ''}")
    manifest = table.read_json(out / "manifest.json") or {}
    table.write_json(out / "manifest.json", {
        **manifest, "judge_identity": judge.identity, "judge_prompt_version": JUDGE_PROMPT_VERSION,
        "judge_config": spec, "judge_code_commit": commit,
        "judged_at": datetime.now(timezone.utc).isoformat(timespec="seconds")})

    judgments = table.run_judgments(reviews, references, judge, out, concurrency=concurrency)
    errors = sum(j["judge_errors"] for j in judgments)
    per_hunk = any(cell.pr is not None for cell in cells)
    if len(cells) == len(all_cells) or per_hunk:  # a per-hunk run is piloted on some PRs: write their table
        if per_hunk:
            rows, hunk_rows, coverage = table.build_hunk_tables(cells, reviews, judgments, references)
            table.write_outputs(out, rows, coverage, hunk_rows)
            log.info(f"wrote {out / 'hunk_table.csv'} ({len(hunk_rows)} rows, one per hunk and level)")
            if len(cells) != len(all_cells):
                log.info(f"the table covers the {len({c.group for c in cells})} PRs of this run, not the whole context file")
        else:
            rows, coverage = table.build_table(reviews, judgments, references)
            table.write_outputs(out, rows, coverage)
        log.info(f"wrote {out / 'table.csv'} ({len(rows)} rows) and {out / 'coverage.csv'} ({len(coverage)} rows)")
        for level in table.summarize_by_level(rows):
            log.info(f"{level['level']}: {level['judged']} judged, mean input tokens {level['mean_input_tokens']}, "
                     f"recall {level['recall_pooled']}, precision {level['precision_pooled']}, "
                     f"mean recall per PR {level['recall_mean_per_pr']}, statuses {level['statuses']}")
    else:
        log.info("the table is written only by a run over every PR; this run judged a selection")
    log.info(f"log file {log_path}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
