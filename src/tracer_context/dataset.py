"""One-file dataset of context levels for every AACR-Bench entry.

`data/aacr_tracer/context_levels.jsonl.gz` holds one JSON line per PR with every level's
review input text, so training and the outcome table load a single file:

    from src.tracer_context.dataset import load_records, level_rows
    records = load_records(path)            # {entry id: record}, latest record per id
    rows = level_rows(records.values())     # one row per (entry, level), with the text

Record fields (format FORMAT):
  id, index, url, repo, language, source_commit, target_commit, merge_base, status
  ("ok"/"failed"), failed_stage, error, build (versions, config hashes, the resume key),
  pr (title, body, edit times), stats (input-side counts usable as selector features),
  known_paths (files named in the trace, for review path normalization), warnings,
  seconds, and levels: {level name: {parent, text, input/pr/diff/context tokens,
  token_budget, items, included_by_kind, included, nesting_ok, config_sha256}}.
  `audit` holds per-entry checks that use the reference comments or the final PR's size:
  for auditing only, never as selector inputs. Reference comment text is never stored.

Writing appends one gzip member per PR, so an interrupted run loses at most the PR in
progress; a cut-off last line is detected and repaired on the next start, and the file is
rewritten in dataset order (latest record per id) at the end of each run.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import shutil
import tempfile
import time
import traceback
import zlib
from collections import Counter
from pathlib import Path
from typing import Callable, Iterable

from src.tracer_context.aacr import (
    BASE_BRANCH, commit_time, edited_after, entry_checks, fetch_pr_text, parse_pr_url, prepare_checkout,
)
from src.tracer_context.build import RENDER_VERSION, build_context, config_sha256, nesting_check
from src.tracer_context.cbm import TRACE_FORMAT, trace, tracer_settings

FORMAT = "aacr-context-levels/v1"


# ---------- file I/O ----------

def append_record(path: Path, record: dict) -> None:
    """Append one record as its own gzip member (gzip readers concatenate members)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    with open(path, "ab") as handle:
        handle.write(gzip.compress(line))
        handle.flush()
        os.fsync(handle.fileno())


def read_records(path: Path) -> tuple[list[dict], bool]:
    """(records in file order, whether the file ended in a cut-off record)."""
    records, truncated = [], False
    if not Path(path).exists():
        return records, truncated
    try:
        with gzip.open(path, "rt", encoding="utf-8") as handle:
            for line in handle:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    truncated = True
                    break
    except (EOFError, gzip.BadGzipFile, zlib.error, OSError):
        truncated = True
    return records, truncated


def latest_by_id(records: Iterable[dict]) -> dict[str, dict]:
    latest = {}
    for record in records:
        latest[record["id"]] = record
    return latest


def load_records(path: Path, status: str | None = "ok") -> dict[str, dict]:
    """Latest record per entry id, optionally only those with the given status."""
    records, _ = read_records(path)
    return {k: v for k, v in latest_by_id(records).items() if status is None or v.get("status") == status}


def rewrite(path: Path, records: Iterable[dict]) -> None:
    """Atomically replace the file with the latest record per id, in dataset order."""
    path = Path(path)
    ordered = sorted(latest_by_id(records).values(), key=lambda r: (r.get("index", 0), r["id"]))
    fd, tmp = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    os.close(fd)
    with gzip.open(tmp, "wt", encoding="utf-8") as handle:
        for record in ordered:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    os.replace(tmp, path)


def level_rows(records: Iterable[dict], include_text: bool = True) -> list[dict]:
    """One row per (entry, level): ids, tokens, item counts, and (optionally) the text."""
    rows = []
    for record in records:
        for name, level in record.get("levels", {}).items():
            row = {"entry": record["id"], "index": record.get("index"), "language": record.get("language"),
                   "level": name, **{k: v for k, v in level.items()
                                     if k not in ("text", "included", "included_by_kind")}}
            if include_text:
                row["text"] = level["text"]
            rows.append(row)
    return rows


# ---------- building one record ----------

def build_key(configs: dict, tool_version: str, tokenizers: dict, record: dict) -> str:
    """Everything that changes a PR's contexts: inputs, tool, trace and render versions,
    every level config and tokenizer. (After changing builder code without bumping
    RENDER_VERSION or TRACE_FORMAT, rebuild with --force.)"""
    first = next(iter(configs.values()))
    key = {"source": record["source_commit"], "target": record["target_commit"], "url": record["githubPrUrl"],
           "tool": tool_version, "trace_format": TRACE_FORMAT, "render": RENDER_VERSION,
           "tracer": tracer_settings(first["tracer"]),
           "levels": [[name, config_sha256(cfg)] for name, cfg in configs.items()],
           "tokenizers": sorted(tokenizers.values()), "format": FORMAT}
    return hashlib.sha256(json.dumps(key, sort_keys=True).encode("utf-8")).hexdigest()


def entry_stats(trace_data: dict, pr: dict | None) -> dict:
    """Input-side counts (from the trace, the diff and the PR text) for selector features."""
    hops = lambda rows: {str(h): n for h, n in sorted(Counter(int(r["hop"]) for r in rows).items())}  # noqa: E731
    hits = trace_data.get("text_hits", {})
    index = trace_data.get("index", {})
    return {
        "changed_files": len(trace_data["changed_files"]),
        "hunks": sum(len(v) for v in trace_data["hunks"].values()),
        "diff_chars": len(trace_data.get("diff", "")),
        "changed_symbols": len(trace_data["seeds"]),
        "changed_symbols_by_label": dict(sorted(Counter(s["label"] for s in trace_data["seeds"]).items())),
        "callers_by_hop": hops(trace_data["callers"]),
        "test_callers_by_hop": hops([r for r in trace_data["callers"] if r.get("via_edge") == "TESTS"]),
        "callees_by_hop": hops(trace_data["callees"]),
        "guide_files": len(trace_data.get("guide_files", [])),
        "search_names": len(hits),
        "search_hits": sum(h["total"] for h in hits.values()),
        "graph_nodes": index.get("nodes"), "graph_edges": index.get("edges"),
        "pr_title_chars": len(pr["title"]) if pr else None, "pr_body_chars": len(pr["body"]) if pr else None,
    }


def trace_paths(trace_data: dict) -> list[str]:
    paths = set(trace_data["changed_files"]) | set(trace_data.get("guide_files", []))
    paths |= {node["file"] for node in trace_data.get("nodes", {}).values() if node.get("file")}
    for hit in trace_data.get("text_hits", {}).values():
        paths |= {path for path, _ in hit.get("hits", [])}
    return sorted(paths)


def _compact_level(report: dict, text: str, parent: str | None, nesting: dict | None) -> dict:
    return {
        "parent": parent, "text": text, "config_sha256": report["config_sha256"],
        "token_budget": report["token_budget"], "input_tokens": report["input_tokens"],
        "pr_tokens": report["pr_tokens"], "diff_tokens": report["diff_tokens"],
        "context_tokens": report["context_tokens"], "items": len(report["included"]),
        "included_by_kind": report["counts"]["included_by_kind"],
        # compact item list: kind, hop, file, first/last line, tokens, name
        "included": [[i["kind"], i["hop"], i["file"], i["start"], i["end"], i["tokens"], i["qn"]]
                     for i in report["included"]],
        "nesting_ok": None if nesting is None else bool(nesting.get("ok")),
    }


def process_entry(entry: dict, configs: dict, counters: dict, *, binary: Path, tool_version: str, repos_dir: Path,
                  scratch_dir: Path, key: str, fetch_depth: int = 50, max_depth: int = 3200,
                  extra_build: dict | None = None) -> dict:
    """Fetch, check, trace and pack every level for one entry; never raises for entry errors."""
    record = entry["record"]
    owner, name, number = parse_pr_url(record["githubPrUrl"])
    out = {"format": FORMAT, "id": entry["id"], "index": entry["index"], "url": record["githubPrUrl"],
           "repo": f"{owner}/{name}", "language": record["project_main_language"],
           "source_commit": record["source_commit"], "target_commit": record["target_commit"],
           "status": "running", "seconds": {},
           "build": {"key": key, "format": FORMAT, "tool_version": tool_version, "trace_format": TRACE_FORMAT,
                     "render_version": RENDER_VERSION,
                     "tokenizers": sorted({label for _, label in counters.values()}),
                     "levels": {n: config_sha256(c) for n, c in configs.items()}, **(extra_build or {})}}
    stage, started = "checkout", time.monotonic()
    try:
        checkout = prepare_checkout(repos_dir, record, depth=fetch_depth, max_depth=max_depth)
        repo = Path(checkout["repo"])
        out.update(merge_base=checkout["merge_base"], fetch_log=checkout["fetch_log"])
        out["seconds"]["checkout"] = round(time.monotonic() - started, 1)

        stage = "checks"
        audit = entry_checks(repo, record, checkout["merge_base"])

        stage, pr = "pr_text", None
        if any(cfg["input"]["pr_text"] for cfg in configs.values()):
            pr = fetch_pr_text(record["githubPrUrl"])
            pr["target_commit_time"] = commit_time(repo, record["target_commit"])
            pr["edited_after_review"] = edited_after(pr["last_edited_at"], pr["target_commit_time"])

        stage, started = "trace", time.monotonic()
        first = next(iter(configs.values()))
        cache = Path(tempfile.mkdtemp(prefix="cbm-cache-", dir=scratch_dir))
        try:
            data = trace(repo, BASE_BRANCH, first["tracer"], binary, cache)
        finally:
            shutil.rmtree(cache, ignore_errors=True)
        out["seconds"]["trace"] = round(time.monotonic() - started, 1)
        out["seconds"]["trace_steps"] = data.get("timings", {})

        stage, started = "pack", time.monotonic()
        reports, levels = {}, {}
        for level_name, cfg in configs.items():  # parents first (checked when loading configs)
            count_tokens, tokenizer = counters[cfg["packing"]["tokenizer"]]
            parent = cfg["level"]["parent"]
            text, report = build_context(data, repo, cfg, count_tokens, tokenizer, pr=pr,
                                         parent_report=reports.get(parent))
            reports[level_name] = report
            nesting = nesting_check(reports[parent], report) if parent else None
            levels[level_name] = _compact_level(report, text, parent, nesting)
        out["seconds"]["pack"] = round(time.monotonic() - started, 1)

        warnings = list(data.get("warnings", []))
        if any(level["nesting_ok"] is False for level in levels.values()):
            warnings.append("a level is missing lines its parent shows")
        out.update(status="ok", levels=levels, stats=entry_stats(data, pr), audit=audit, pr=pr,
                   known_paths=trace_paths(data), warnings=warnings, graph=data.get("index", {}))
    except KeyboardInterrupt:
        raise
    except Exception as error:  # noqa: BLE001 - recorded; the run continues with the next entry
        out.update(status="failed", failed_stage=stage, error=f"{type(error).__name__}: {error}"[:2000],
                   traceback=traceback.format_exc()[-4000:])
    return out


# ---------- the run ----------

def repo_dir_name(entry: dict) -> str:
    owner, name, _ = parse_pr_url(entry["record"]["githubPrUrl"])
    return f"{owner}__{name}"


def group_by_repo(entries: list[dict]) -> list[tuple[str, list[dict]]]:
    """Entries grouped by repository, groups in order of their first entry."""
    groups: dict[str, list[dict]] = {}
    for entry in sorted(entries, key=lambda e: e["index"]):
        groups.setdefault(repo_dir_name(entry), []).append(entry)
    return list(groups.items())


def run(entries: list[dict], process: Callable[[dict], dict], key_of: Callable[[dict], str], out_path: Path,
        repos_dir: Path, keep_repos: bool = False, force: bool = False,
        report: Callable[[dict, str, float], None] | None = None) -> Counter:
    """Build missing or outdated records, one repository at a time, deleting each clone after
    its last entry (unless keep_repos). Returns counts of ok / failed / skipped."""
    out_path, repos_dir = Path(out_path), Path(repos_dir)
    existing, truncated = read_records(out_path)
    if truncated:  # an interrupted append: keep the complete records only
        rewrite(out_path, existing)
    done = latest_by_id(existing)
    counts: Counter = Counter()
    try:
        for repo_name, group in group_by_repo(entries):
            for entry in group:
                previous = done.get(entry["id"])
                if (not force and previous and previous.get("status") == "ok"
                        and previous.get("build", {}).get("key") == key_of(entry)):
                    counts["skipped"] += 1
                    if report:
                        report(previous, "skipped", 0.0)
                    continue
                started = time.monotonic()
                record = process(entry)
                append_record(out_path, record)
                done[entry["id"]] = record
                counts[record["status"]] += 1
                if report:
                    report(record, record["status"], time.monotonic() - started)
            if not keep_repos:
                shutil.rmtree(repos_dir / repo_name, ignore_errors=True)
    finally:
        records, _ = read_records(out_path)
        if records:
            rewrite(out_path, records)
    return counts
