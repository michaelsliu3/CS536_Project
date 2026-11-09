"""Build tracer contexts for many AACR-Bench entries, one PR at a time.

For each selected entry: fetch the repository at the PR's base and reviewed commits (one
clone per repository, reused), run per-entry checks, trace once with codebase-memory-mcp,
then pack one context per --config. Results already built with the same inputs are reused,
so the command can be re-run to resume. A failed entry is recorded and the batch continues.

  <work>/repos/<owner>__<repo>/          git clones
  <work>/entries/<id>/status.json        outcome, checks, timings, per-config token counts
  <work>/entries/<id>/trace.json         the tracer output (shared by all configs)
  <work>/entries/<id>/<config>/context.md, selection.json
  <work>/summary.csv                     one row per entry with a status.json

Reference comments are never written to these outputs.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.tracer_context.aacr import (  # noqa: E402
    BASE_BRANCH, FETCH_VERSION, commit_time, edited_after, entry_checks, fetch_pr_text, file_sha256,
    github_merge_base, load_entries, prepare_checkout, select_entries,
)
from src.tracer_context.build import RENDER_VERSION, build_context, config_sha256, nesting_check  # noqa: E402
from src.tracer_context.cbm import TRACE_FORMAT, tracer_settings, trace  # noqa: E402
from src.tracer_context.pipeline import RUNNER_WORK  # noqa: E402
from src.tracer_context.runtime import load_level_configs, make_counter, write_json  # noqa: E402

DEFAULT_DATASET = PROJECT_ROOT / "data" / "aacr" / "positive_samples.json"
DEFAULT_WORK = PROJECT_ROOT / RUNNER_WORK  # scripts/dataset/clean_dataset.py also deletes its repos/


def read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def trace_key(settings: dict, tool_version: str, record: dict) -> str:
    key = {"tracer": settings, "tool": tool_version, "format": TRACE_FORMAT, "fetch": FETCH_VERSION,
           "source": record["source_commit"], "target": record["target_commit"]}
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode("utf-8")).hexdigest()


load_configs = load_level_configs  # shared with scripts/dataset/build_dataset.py


def run_entry(entry, configs, counters, args, binary, tool_version, dataset_sha) -> dict:
    record = entry["record"]
    out = args.work / "entries" / entry["id"]
    out.mkdir(parents=True, exist_ok=True)
    previous = read_json(out / "status.json") or {}
    first = next(iter(configs.values()))
    key = trace_key(tracer_settings(first["tracer"]), tool_version, record)
    status = {"id": entry["id"], "index": entry["index"], "url": record["githubPrUrl"],
              "language": record["project_main_language"], "source_commit": record["source_commit"],
              "target_commit": record["target_commit"], "dataset_sha256": dataset_sha,
              "tool_version": tool_version, "trace_key": key, "status": "running", "seconds": {}}
    stage = "merge_base"
    try:
        started = time.monotonic()
        merge_base = github_merge_base(record["githubPrUrl"], record["source_commit"], record["target_commit"])
        stage = "checkout"
        checkout = prepare_checkout(args.work / "repos", record, merge_base, depth=args.fetch_depth,
                                    max_depth=args.max_depth)
        repo = Path(checkout["repo"])
        status.update(merge_base=checkout["merge_base"], fetch_log=checkout["fetch_log"])
        status["seconds"]["checkout"] = round(time.monotonic() - started, 1)

        stage = "checks"
        status["checks"] = entry_checks(repo, record, checkout["merge_base"])

        stage = "pr_text"
        pr, pr_reused = None, True
        if any(cfg["input"]["pr_text"] for cfg in configs.values()):
            pr = read_json(out / "pr.json")
            if args.force or not pr or pr.get("url") != record["githubPrUrl"]:
                pr = fetch_pr_text(record["githubPrUrl"])
                pr["target_commit_time"] = commit_time(repo, record["target_commit"])
                pr["edited_after_review"] = edited_after(pr["last_edited_at"], pr["target_commit_time"])
                write_json(out / "pr.json", pr)
                pr_reused = False
            status["pr_text"] = {"title_chars": len(pr["title"]), "body_chars": len(pr["body"]),
                                 "last_edited_at": pr["last_edited_at"],
                                 "edited_after_review": pr["edited_after_review"]}

        stage = "trace"
        started = time.monotonic()
        trace_path = out / "trace.json"
        data = read_json(trace_path)
        if args.force or previous.get("trace_key") != key or not data or data.get("head") != record["target_commit"]:
            cache = out / "index_cache" if args.keep_index else Path(tempfile.mkdtemp(prefix="cbm-cache-", dir=out))
            try:
                data = trace(repo, BASE_BRANCH, first["tracer"], binary, cache)
            finally:
                if not args.keep_index:
                    shutil.rmtree(cache, ignore_errors=True)
            write_json(trace_path, data)
            status["trace_reused"] = False
        else:
            status["trace_reused"] = True
        status["seconds"]["trace"] = round(time.monotonic() - started, 1)
        status["warnings"] = data.get("warnings", [])
        status["graph"] = data.get("index", {})

        stage = "pack"
        started = time.monotonic()
        status["configs"], status["nesting"] = {}, {}
        reports, reused = {}, {}
        for name, cfg in configs.items():  # parents come first (checked in load_configs)
            count_tokens, tokenizer = counters[cfg["packing"]["tokenizer"]]
            parent = cfg["level"]["parent"]
            parent_report = reports[parent] if parent else None
            target_dir = out / name
            saved = read_json(target_dir / "selection.json")
            reusable = bool(
                not args.force and status["trace_reused"] and pr_reused and saved
                and (parent is None or reused[parent])
                and saved.get("config_sha256") == config_sha256(cfg) and saved.get("tokenizer") == tokenizer
                and saved.get("render_version") == RENDER_VERSION and (target_dir / "context.md").is_file()
                and saved.get("parent_config_sha256") == (parent_report["config_sha256"] if parent_report else None))
            if not reusable:
                text, saved = build_context(data, repo, cfg, count_tokens, tokenizer, pr=pr,
                                            parent_report=parent_report)
                target_dir.mkdir(exist_ok=True)
                (target_dir / "context.md").write_text(text + ("\n" if text else ""), encoding="utf-8")
                write_json(target_dir / "selection.json", saved)
            reports[name], reused[name] = saved, reusable
            status["configs"][name] = {
                "config_sha256": saved["config_sha256"], "tokenizer": saved["tokenizer"],
                "token_budget": saved["token_budget"], "input_tokens": saved["input_tokens"],
                "pr_tokens": saved["pr_tokens"], "diff_tokens": saved["diff_tokens"],
                "context_tokens": saved["context_tokens"], "items": len(saved["included"]),
                "included_by_kind": saved["counts"]["included_by_kind"], "reused": reusable}
            if parent:
                status["nesting"][name] = nesting_check(parent_report, saved)
        status["seconds"]["pack"] = round(time.monotonic() - started, 1)
        if not all(check["ok"] for check in status["nesting"].values()):
            status["warnings"] = status["warnings"] + ["a level is missing lines its parent shows"]
        status["status"] = "ok"
    except KeyboardInterrupt:
        status.update(status="interrupted", failed_stage=stage)
        write_json(out / "status.json", status)
        raise
    except Exception as error:  # noqa: BLE001 - record any failure and continue the batch
        status.update(status="failed", failed_stage=stage, error=f"{type(error).__name__}: {error}"[:2000])
        if args.verbose:
            traceback.print_exc()
    write_json(out / "status.json", status)
    return status


def write_summary(work: Path, config_names) -> Path:
    rows = []
    for path in sorted((work / "entries").glob("*/status.json")):
        status = read_json(path) or {}
        checks = status.get("checks", {})
        comment = checks.get("comment_lines", {})
        row = {"id": status.get("id"), "index": status.get("index"), "language": status.get("language"),
               "status": status.get("status"), "failed_stage": status.get("failed_stage", ""),
               "diff_lines": checks.get("diff_lines"), "change_line_count": checks.get("change_line_count"),
               "comments_ok": comment.get("ok"),
               "comments_bad": (comment.get("missing_file") or 0) + (comment.get("out_of_range") or 0),
               "comments_outside_diff": comment.get("right_side_outside_diff"),
               "graph_nodes": status.get("graph", {}).get("nodes"),
               "trace_seconds": status.get("seconds", {}).get("trace"),
               "tool_warnings": len(status.get("warnings", []))}
        row["nesting_ok"] = all(c.get("ok") for c in status.get("nesting", {}).values()) if status.get("nesting") else ""
        for name in config_names:
            result = status.get("configs", {}).get(name, {})
            row[f"{name}_input_tokens"] = result.get("input_tokens")
            row[f"{name}_context_tokens"] = result.get("context_tokens")
            row[f"{name}_items"] = result.get("items")
        rows.append(row)
    path = work / "summary.csv"
    if rows:
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
    return path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET, help="AACR-Bench positive_samples.json")
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK, help="Folder for clones and outputs")
    parser.add_argument("--config", type=Path, action="append",
                        help="Config file (repeatable; all must share one `tracer` section). "
                             "Default: configs/tracer_context.yaml")
    parser.add_argument("--entries", help="Comma-separated entry IDs (owner__repo__number) or file indices")
    parser.add_argument("--languages", help="Comma-separated main languages, e.g. Go,TypeScript")
    parser.add_argument("--limit", type=int, help="At most this many selected entries")
    parser.add_argument("--all", action="store_true", help="Run every entry (required when nothing is selected)")
    parser.add_argument("--list", action="store_true", help="Print the selected entries and their status, then exit")
    parser.add_argument("--force", action="store_true", help="Rebuild even when saved results match")
    parser.add_argument("--keep-index", action="store_true", help="Keep each entry's graph database")
    parser.add_argument("--stop-on-error", action="store_true", help="Stop at the first failed entry")
    parser.add_argument("--fetch-depth", type=int, default=50, help="Initial history depth to fetch")
    parser.add_argument("--max-depth", type=int, default=3200, help="Give up finding a merge base beyond this")
    parser.add_argument("--verbose", action="store_true", help="Print tracebacks for failed entries")
    args = parser.parse_args()
    args.work = args.work.resolve()

    entries = load_entries(args.dataset)
    selected = select_entries(entries, ids=args.entries.split(",") if args.entries else None,
                              languages=args.languages.split(",") if args.languages else None, limit=args.limit)
    if args.list:
        for entry in selected:
            status = read_json(args.work / "entries" / entry["id"] / "status.json") or {}
            print(f"{entry['index']:>4}  {entry['id']:<55} {entry['record']['project_main_language']:<11} "
                  f"{entry['record']['change_line_count']:>5} lines  {status.get('status', '-')}")
        print(f"{len(selected)} entries")
        return
    if not (args.entries or args.languages or args.limit is not None or args.all):
        parser.error("select entries with --entries, --languages or --limit, or pass --all to run every entry")

    configs = load_configs(args.config or [PROJECT_ROOT / "configs" / "tracer_context.yaml"])
    counters = {}
    for cfg in configs.values():  # load tokenizers before any fetching or indexing
        name = cfg["packing"]["tokenizer"]
        counters.setdefault(name, make_counter(name))
    first = next(iter(configs.values()))
    binary = Path(first["tracer"]["binary"])
    binary = binary if binary.is_absolute() else PROJECT_ROOT / binary
    tool_version = subprocess.run([str(binary), "--version"], capture_output=True, text=True,
                                  check=True).stdout.strip()
    dataset_sha = file_sha256(args.dataset)

    print(f"{len(selected)} entries, configs {list(configs)}, {tool_version}, work {args.work}", flush=True)
    counts = {}
    for position, entry in enumerate(selected, 1):
        started = time.monotonic()
        status = run_entry(entry, configs, counters, args, binary, tool_version, dataset_sha)
        counts[status["status"]] = counts.get(status["status"], 0) + 1
        detail = ("" if status["status"] == "ok"
                  else f" - {status.get('failed_stage')}: {status.get('error', '')[:300]}")
        print(f"[{position}/{len(selected)}] {entry['id']} ({status['language']}) {status['status']} "
              f"in {time.monotonic() - started:.0f}s{detail}", flush=True)
        for name, c in status.get("configs", {}).items():
            nesting = status.get("nesting", {}).get(name)
            print(f"    {name:<16} input {c['input_tokens']:>7} tok (context {c['context_tokens']:>6}/"
                  f"{c['token_budget']:>6}), {c['items']:>3} items"
                  + ("" if not nesting else "  nested ok" if nesting["ok"]
                     else f"  NESTING: {nesting['missing_lines']} parent lines missing"), flush=True)
        if status["status"] != "ok" and args.stop_on_error:
            break
    summary = write_summary(args.work, list(configs))
    print(f"done: {counts}; summary {summary}")


if __name__ == "__main__":
    main()
