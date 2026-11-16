"""One-file dataset of context levels for every AACR-Bench entry.

`<root>/context_levels.jsonl` (e.g. data/aacr/, built by scripts/dataset/build_dataset.py) holds one
JSON line per PR (plain JSON Lines) with every level's review input text, so training and the
outcome table load a single file:

    from src.tracer_context.dataset import load_records, level_rows
    records = load_records(path)            # {entry id: record}, latest record per id
    rows = level_rows(records.values())     # one row per (entry, level), with the text

Record fields (format FORMAT):
  id, index, url, repo, language, source_commit, target_commit, merge_base (GitHub's merge base
  for source...target, where the diff starts), fetch_log, status
  ("ok"/"failed"), failed_stage, error, build (versions, config hashes, the resume key),
  pr (title, body, edit times), stats (input-side counts usable as selector features),
  known_paths (files named in the trace, for review path normalization), warnings,
  seconds, and levels: {level name: {parent, text, input/pr/diff/context tokens,
  token_budget (the context's room in this PR), max_input_tokens, capped, fits,
  same_as_parent, items, included_by_kind, included, nesting_ok, config_sha256}}.
  `audit` holds per-entry checks that use the reference comments or the final PR's size:
  for auditing only, never as selector inputs. Reference comment text is never stored.

Writing appends one synced line per PR, so an interrupted run loses at most the PR in
progress; a cut-off last line is detected and repaired on the next start, and the file is
rewritten in dataset order (latest record per id) at the end of each run. Files written
before November 8 were gzip-compressed (`context_levels.jsonl.gz`); `read_records` still
reads them. `write_json` writes a readable copy as one pretty-printed JSON document
(`.json`, e.g. the committed sample in data/aacr_sample/), which `read_records` also reads.
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
    BASE_BRANCH, FETCH_VERSION, CommitGoneError, commit_time, edited_after, entry_checks, fetch_pr_text,
    github_merge_base, parse_pr_url, prepare_checkout,
)
from src.tracer_context.build import RENDER_VERSION, build_context, config_sha256, nesting_check
from src.tracer_context.cbm import TRACE_FORMAT, trace, tracer_settings
from src.tracer_context.hunks import HUNK_VERSION, HunkTracer, hunk_id

FORMAT = "aacr-context-levels/v1"
# One line per PR, like FORMAT, but the levels are packed per diff hunk (`unit="hunk"`): a record has
# `hunks` (a list, in diff order) where FORMAT has `levels`. Each hunk has id (`<PR id>__h0001`),
# index, file, old_file, deleted_file, header, old/new start and count, changed_symbols, stats and
# its own `levels`. A level identical to its parent stores no text (`text: null`); `hunk_units`
# fills it in. `hunk_checks` counts how the PR was split and how well the per-hunk walks repeat
# the tool's walk.
FORMAT_HUNK = "aacr-hunk-context-levels/v1"
UNITS = ("pr", "hunk")


# ---------- file I/O ----------

def _line(record: dict) -> str:
    return json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"


def append_record(path: Path, record: dict) -> None:
    """Append one record as one line, written and synced in one go."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab+") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell():  # never glue a record onto a last line that lacks its newline
            handle.seek(-1, os.SEEK_END)
            if handle.read(1) != b"\n":
                handle.write(b"\n")
        handle.write(_line(record).encode("utf-8"))
        handle.flush()
        os.fsync(handle.fileno())


def read_records(path: Path) -> tuple[list[dict], bool]:
    """(records in file order, whether the file ended in a cut-off record). Reads plain JSON
    Lines; a `.json` path as one JSON document ({id: record} from `write_json`, or a list); a
    `.gz` path as gzip (the format used before November 8)."""
    records, truncated = [], False
    path = Path(path)
    if not path.exists():
        return records, truncated
    if path.suffix == ".json":
        document = json.loads(path.read_text(encoding="utf-8"))
        return (list(document.values()) if isinstance(document, dict) else list(document)), truncated
    if path.suffix == ".gz":
        try:
            with gzip.open(path, "rt", encoding="utf-8") as handle:
                for line in handle:
                    records.append(json.loads(line))
        except (EOFError, gzip.BadGzipFile, zlib.error, OSError, json.JSONDecodeError):
            truncated = True
        return records, truncated
    with open(path, "rb") as handle:
        for raw in handle:  # streamed line by line: the file can be large
            if not raw.strip():
                continue
            try:
                records.append(json.loads(raw))
            except ValueError:  # a line cut off by an interrupted write
                truncated = True
                break
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


def _in_dataset_order(records: Iterable[dict]) -> list[dict]:
    return sorted(latest_by_id(records).values(), key=lambda r: (r.get("index", 0), r["id"]))


def _replace_atomically(path: Path, write: Callable) -> None:
    """Write to a temporary file next to `path`, then swap it in."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    os.close(fd)
    umask = os.umask(0)
    os.umask(umask)
    os.chmod(tmp, 0o666 & ~umask)  # mkstemp makes the file private; give it normal permissions
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            write(handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:  # e.g. Ctrl+C mid-rewrite: the old file stays, the partial copy goes
        Path(tmp).unlink(missing_ok=True)
        raise


def rewrite(path: Path, records: Iterable[dict]) -> None:
    """Atomically replace the JSON Lines file with the latest record per id, in dataset order."""
    ordered = _in_dataset_order(records)
    _replace_atomically(path, lambda handle: handle.writelines(_line(r) for r in ordered))


def write_json(path: Path, records: Iterable[dict]) -> None:
    """Write the latest record per id as one pretty-printed JSON document, {id: record} in
    dataset order, e.g. a readable copy of a few PRs. `load_records` reads it back."""
    ordered = _in_dataset_order(records)
    document = {r["id"]: r for r in ordered}
    _replace_atomically(path, lambda handle: handle.write(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n"))


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

def build_key(configs: dict, tool_version: str, tokenizers: dict, record: dict, unit: str = "pr") -> str:
    """Everything that changes a PR's contexts: inputs, how the checkout finds the merge base,
    tool, trace and render versions, every level config and tokenizer, and for per-hunk levels
    how the PR is split. (After changing builder code without bumping FETCH_VERSION,
    RENDER_VERSION, TRACE_FORMAT or HUNK_VERSION, rebuild with --force.)"""
    first = next(iter(configs.values()))
    key = {"source": record["source_commit"], "target": record["target_commit"], "url": record["githubPrUrl"],
           "fetch": FETCH_VERSION, "tool": tool_version, "trace_format": TRACE_FORMAT, "render": RENDER_VERSION,
           "tracer": tracer_settings(first["tracer"]),
           "levels": [[name, config_sha256(cfg)] for name, cfg in configs.items()],
           "tokenizers": sorted(tokenizers.values()), "format": FORMAT}
    if unit != "pr":  # a per-PR key is unchanged
        key.update(format=FORMAT_HUNK, unit=unit, hunk_version=HUNK_VERSION)
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


def pack_levels(data: dict, repo: Path, configs: dict, counters: dict, pr: dict | None = None) -> dict:
    """Pack every level from one trace, parents first (checked when loading configs), as the
    compact per-level dicts stored in a record; marks levels identical to their parent."""
    reports, levels = {}, {}
    for level_name, cfg in configs.items():
        count_tokens, tokenizer = counters[cfg["packing"]["tokenizer"]]
        parent = cfg["level"]["parent"]
        text, report = build_context(data, repo, cfg, count_tokens, tokenizer, pr=pr, parent_report=reports.get(parent))
        reports[level_name] = report
        nesting = nesting_check(reports[parent], report) if parent else None
        same = parent is not None and text == levels[parent]["text"]
        levels[level_name] = _compact_level(report, text, parent, nesting, same)
    return levels


def _memo(count: Callable[[str], int], max_chars: int = 200_000, max_total: int = 300_000_000) -> Callable[[str], int]:
    """`count` with its answers remembered. A PR's hunks show many of the same code blocks, and
    counting tokens is most of the packing time. Long texts (whole inputs) are not kept."""
    cache: dict[str, int] = {}
    total = 0

    def counted(text: str) -> int:
        nonlocal total
        if len(text) > max_chars:
            return count(text)
        if text not in cache:
            if total > max_total:
                cache.clear()
                total = 0
            cache[text] = count(text)
            total += len(text)
        return cache[text]

    return counted


_HUNK_STATS = ("changed_symbols", "changed_symbols_by_label", "callers_by_hop", "test_callers_by_hop",
               "callees_by_hop", "search_names", "search_hits")


def pack_hunks(data: dict, repo: Path, configs: dict, counters: dict, pr: dict | None, entry_id: str) -> tuple[list, dict]:
    """(one dict per diff hunk with its own levels, counts of how the PR was split).

    `data` is a trace made with per_seed_edges. Each hunk's levels are packed from the trace
    restricted to that hunk (hunks.HunkTracer), by the same code as a PR's levels."""
    tracer = HunkTracer(data)
    cached = {name: (_memo(count), label) for name, (count, label) in counters.items()}
    hunks, in_some_hunk = [], set()
    for hunk in tracer.hunks:
        sub = tracer.restrict(hunk)
        in_some_hunk |= {s["qn"] for s in sub["seeds"]}
        levels = pack_levels(sub, repo, configs, cached, pr)
        for level in levels.values():
            if level["same_as_parent"]:
                level["text"] = None  # the parent's text; hunk_units fills it in
        stats = entry_stats(sub, None)
        body = hunk.body.split("\n")[1:]
        hunks.append({
            "id": hunk_id(entry_id, hunk.index), "index": hunk.index, "file": hunk.file, "old_file": hunk.old_file,
            "deleted_file": hunk.deleted_file, "header": hunk.header,
            "old_start": hunk.old_start, "old_count": hunk.old_count,
            "new_start": hunk.new_start, "new_count": hunk.new_count,
            "stats": {**{k: stats[k] for k in _HUNK_STATS}, "diff_chars": len(hunk.text),
                      "added_lines": sum(line.startswith("+") for line in body),
                      "removed_lines": sum(line.startswith("-") for line in body)},
            "levels": levels})
    all_seeds = {s["qn"] for s in data["seeds"]}
    checks = {"hunks": len(hunks), "hunks_with_changed_symbols": sum(1 for h in hunks if h["stats"]["changed_symbols"]),
              "changed_symbols": len(all_seeds), "changed_symbols_in_no_hunk": len(all_seeds - in_some_hunk),
              "links": tracer.check_links()}
    return hunks, checks


def check_unit(records: Iterable[dict], unit: str) -> None:
    """Raise ValueError if a record was built for the other unit, so a per-hunk build can never
    replace the per-PR records of a file (or the other way round)."""
    wrong = sorted(r["id"] for r in records if r.get("status") == "ok" and ("hunks" in r) != (unit == "hunk"))
    if wrong:
        other = "per-hunk" if unit == "pr" else "per-PR"
        raise ValueError(f"this file holds {other} records ({len(wrong)}, e.g. {wrong[0]}); a "
                         f"{'per-PR' if unit == 'pr' else 'per-hunk'} build must not write into it")


def hunk_units(records: Iterable[dict]) -> list[dict]:
    """One record-like dict per hunk of FORMAT_HUNK records, in dataset and diff order, for code
    that reads a record's `levels`: id (the hunk's), pr_id, index (the PR's), hunk (its
    position and lines), language, known_paths, and levels with every text filled in."""
    units = []
    for record in records:
        for hunk in record.get("hunks", []):
            levels = {}
            for name, level in hunk["levels"].items():
                text = level["text"] if level["text"] is not None else levels[level["parent"]]["text"]
                levels[name] = {**level, "text": text}
            units.append({"id": hunk["id"], "pr_id": record["id"], "index": record.get("index"),
                          "language": record.get("language"), "known_paths": record.get("known_paths", []),
                          "hunk": {k: v for k, v in hunk.items() if k != "levels"}, "levels": levels})
    return units


def _compact_level(report: dict, text: str, parent: str | None, nesting: dict | None,
                   same_as_parent: bool = False) -> dict:
    other = report.get("other_hunks")  # per-hunk levels that show the PR's other hunks
    extra = {} if other is None else {"other_hunks_total": other["total"], "other_hunks_shown": other["shown"],
                                      "other_tokens": other["tokens"]}
    return {
        **extra,
        # where the PR text, the diff, the other hunks and the repository context sit in `text`
        "sections": report["sections"],
        "parent": parent, "text": text, "config_sha256": report["config_sha256"],
        "token_budget": report["token_budget"], "max_input_tokens": report.get("max_input_tokens"),
        "input_tokens": report["input_tokens"],
        # capped: items were left out for lack of room; fits: within max_input_tokens;
        # same_as_parent: identical text, so the same review (the outcome table reviews it once)
        "capped": report.get("capped", False), "fits": report.get("fits", True), "same_as_parent": same_as_parent,
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
                  extra_build: dict | None = None, unit: str = "pr") -> dict:
    """Fetch, check, trace and pack every level for one entry; never raises for entry errors.
    With `unit="hunk"` the levels are packed for each diff hunk (a FORMAT_HUNK record)."""
    if unit not in UNITS:
        raise ValueError(f"unit must be one of {UNITS}, not {unit!r}")
    record = entry["record"]
    owner, name, number = parse_pr_url(record["githubPrUrl"])
    file_format = FORMAT if unit == "pr" else FORMAT_HUNK
    out = {"format": file_format, "id": entry["id"], "index": entry["index"], "url": record["githubPrUrl"],
           "repo": f"{owner}/{name}", "language": record["project_main_language"],
           "source_commit": record["source_commit"], "target_commit": record["target_commit"],
           "status": "running", "seconds": {},
           "build": {"key": key, "format": file_format, "fetch_version": FETCH_VERSION, "tool_version": tool_version,
                     "trace_format": TRACE_FORMAT, "render_version": RENDER_VERSION,
                     **({} if unit == "pr" else {"unit": unit, "hunk_version": HUNK_VERSION}),
                     "tokenizers": sorted({label for _, label in counters.values()}),
                     "levels": {n: config_sha256(c) for n, c in configs.items()}, **(extra_build or {})}}
    stage, started = "merge_base", time.monotonic()
    try:
        merge_base = github_merge_base(record["githubPrUrl"], record["source_commit"], record["target_commit"])
        stage = "checkout"
        checkout = prepare_checkout(repos_dir, record, merge_base, depth=fetch_depth, max_depth=max_depth)
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
            data = trace(repo, BASE_BRANCH, first["tracer"], binary, cache, per_seed_edges=unit == "hunk")
        finally:
            shutil.rmtree(cache, ignore_errors=True)
        out["seconds"]["trace"] = round(time.monotonic() - started, 1)
        out["seconds"]["trace_steps"] = data.get("timings", {})

        stage, started = "pack", time.monotonic()
        if unit == "hunk":
            hunks, checks = pack_hunks(data, repo, configs, counters, pr, entry["id"])
            packed, all_levels = {"hunks": hunks, "hunk_checks": checks}, [lv for h in hunks for lv in h["levels"].values()]
        else:
            levels = pack_levels(data, repo, configs, counters, pr)
            packed, all_levels = {"levels": levels}, list(levels.values())
        out["seconds"]["pack"] = round(time.monotonic() - started, 1)

        warnings = list(data.get("warnings", []))
        if any(level["nesting_ok"] is False for level in all_levels):
            warnings.append("a level is missing lines its parent shows")
        if any(not level["fits"] for level in all_levels):
            warnings.append("the PR text and diff alone exceed max_input_tokens")
        if unit == "hunk":
            if not packed["hunks"]:
                warnings.append("the diff has no hunk to review")
            if checks["changed_symbols_in_no_hunk"]:
                warnings.append(f"{checks['changed_symbols_in_no_hunk']} changed symbols overlap no hunk's changed lines")
            for walk, counts in checks["links"].items():
                if counts["not_walked"] or counts["only_walked"] or counts["hop_differs"]:
                    warnings.append(f"per-hunk {walk}: the walk over the kept links differs from the tool's ({counts})")
        out.update(status="ok", **packed, stats=entry_stats(data, pr), audit=audit, pr=pr,
                   known_paths=trace_paths(data), warnings=warnings, graph=data.get("index", {}))
    except KeyboardInterrupt:
        raise
    except Exception as error:  # noqa: BLE001 - recorded; the run continues with the next entry
        out.update(status="failed", failed_stage=stage, error=f"{type(error).__name__}: {error}"[:2000],
                   traceback=traceback.format_exc()[-4000:], retryable=not isinstance(error, CommitGoneError))
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


class StopBuild(Exception):
    """Raised by a `before_repo` hook to end the run cleanly (e.g. low disk space)."""


def is_current(record: dict | None, key: str) -> bool:
    return bool(record and record.get("status") == "ok" and record.get("build", {}).get("key") == key)


def run(entries: list[dict], process: Callable[[dict], dict], key_of: Callable[[dict], str], out_path: Path,
        repos_dir: Path, keep_repos: bool = False, force: bool = False,
        report: Callable[[dict, str, float], None] | None = None, retries: int = 0,
        retry_wait: float = 30.0, before_repo: Callable[[str, list[dict]], None] | None = None,
        on_retry: Callable[[dict, int], None] | None = None, sleep: Callable[[float], None] = time.sleep) -> Counter:
    """Build missing or outdated records, one repository at a time, deleting each clone after
    its last entry (unless keep_repos). Returns counts of ok / failed / skipped.

    - Resumes: entries whose latest record is ok with the current build key are skipped.
    - Retries: a failed entry is retried up to `retries` times after `retry_wait` seconds
      (doubling); a failure while fetching first deletes the clone, which may be half-written.
      A record with `retryable: false` (its commits are gone from GitHub) is not retried.
    - `before_repo(name, group)` runs before each repository with work to do; it may raise
      StopBuild to stop cleanly. The output file is tidied up however the run ends.
    """
    out_path, repos_dir = Path(out_path), Path(repos_dir)
    existing, truncated = read_records(out_path)
    if truncated:  # an interrupted append: keep the complete records only
        rewrite(out_path, existing)
    done = latest_by_id(existing)
    counts: Counter = Counter()
    try:
        for repo_name, group in group_by_repo(entries):
            todo = [e for e in group if force or not is_current(done.get(e["id"]), key_of(e))]
            for entry in group:
                if entry not in todo:
                    counts["skipped"] += 1
                    if report:
                        report(done[entry["id"]], "skipped", 0.0)
            if not todo:
                continue
            if before_repo:
                before_repo(repo_name, todo)
            try:
                for entry in todo:
                    started = time.monotonic()
                    record = process(entry)
                    for attempt in range(1, retries + 1):
                        if record["status"] == "ok" or record.get("retryable") is False:
                            break
                        if on_retry:
                            on_retry(record, attempt)
                        if record.get("failed_stage") == "checkout":
                            shutil.rmtree(repos_dir / repo_name, ignore_errors=True)
                        sleep(retry_wait * 2 ** (attempt - 1))
                        record = process(entry)
                    append_record(out_path, record)
                    done[entry["id"]] = record
                    counts[record["status"]] += 1
                    if report:
                        report(record, record["status"], time.monotonic() - started)
            finally:
                if not keep_repos:
                    shutil.rmtree(repos_dir / repo_name, ignore_errors=True)
    finally:
        records, _ = read_records(out_path)
        if records:
            rewrite(out_path, records)
    return counts
