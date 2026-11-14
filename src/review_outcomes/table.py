"""Build the offline (PR x level) outcome table from the context-level dataset file
(`<root>/context_levels.jsonl`, e.g. data/aacr/, written by scripts/dataset/build_dataset.py).

Layout under `out_dir` (one folder per run, e.g. data/aacr/outcomes/<run_name>/):
  manifest.json                         run settings, prompt and dataset hashes
  reviews/<entry>/<level>.json          prompt hashes, backend identity, raw reply, parsed findings
  judgments/<entry>/<level>.json        AACR judge records per generated comment, per-reference flags
  table.csv / table.json                one row per (entry, level); table.json is one JSON array
  coverage.csv                          one row per (entry, level, reference): covered or not
  logs/<stage>_<time>.log               the run's log, when start_run_log() was called (the notebook does)

Every record carries a cache key over all behavior-affecting inputs, so re-running resumes:
matching cells are reused, changed ones are redone. Reference comments are read only in the
judging stage; they never reach the reviewer.
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from src.review_outcomes.judge import score_review
from src.review_outcomes.parse import PARSER_VERSION, parse_review
from src.review_outcomes.prompts import JUDGE_PROMPT_VERSION, REVIEW_PROMPT_VERSION, prompt_sha256, review_messages


log = logging.getLogger("review_outcomes")
SUMMARY_EVERY = 300.0  # seconds between progress summaries in the log


def start_run_log(out_dir: Path, stage: str = "reviews", console_level: int = logging.WARNING) -> Path:
    """Write this module's log to <out_dir>/logs/<stage>_<time>.log, and show warnings and errors
    on the console (under the notebook cell; a script passes logging.INFO to show every line).
    Calling it again starts a new file."""
    path = Path(out_dir) / "logs" / f"{stage}_{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    for handler in [h for h in log.handlers if getattr(h, "run_log", False)]:
        log.removeHandler(handler)
        handler.close()
    file_handler = logging.FileHandler(path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%H:%M:%S"))
    for handler in (file_handler, console):
        handler.run_log = True
        log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.propagate = False
    log.info(f"log file {path}")
    return path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _duration(seconds: float) -> str:
    seconds = int(max(seconds, 0))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}h{rest // 60:02d}m" if hours else f"{rest // 60}m{rest % 60:02d}s"


class _ReviewProgress:
    """Counts finished model calls and logs each one, every problem, and a periodic summary."""

    def __init__(self, calls: int, tokens: int):
        self.calls, self.tokens = calls, tokens
        self.done = self.done_tokens = self.output_tokens = 0
        self.statuses: dict[str, int] = {}
        self.truncated = 0
        self.started = self.last_summary = time.monotonic()
        self.lock = threading.Lock()

    def record(self, record: dict) -> None:
        reply, status = record.get("reply") or {}, record["status"]
        where = f"{record['entry']} {record['level']}"
        with self.lock:
            self.done += 1
            self.done_tokens += record.get("input_tokens") or 0
            self.output_tokens += reply.get("completion_tokens") or 0
            self.statuses[status] = self.statuses.get(status, 0) + 1
            self.truncated += bool(record.get("truncated"))
            count, due = self.done, time.monotonic() - self.last_summary >= SUMMARY_EVERY
            if due:
                self.last_summary = time.monotonic()
        if status == "error":
            log.error(f"review {count}/{self.calls} {where}: FAILED, {record.get('input_tokens')} input tokens: "
                      f"{record.get('error')} (kept as an error; the next run retries it)")
        else:
            findings = len((record.get("parsed") or {}).get("findings", []))
            log.info(f"review {count}/{self.calls} {where}: {status}, {record.get('input_tokens'):,} input tokens "
                     f"(prompt {reply.get('prompt_tokens')}), {reply.get('completion_tokens')} output tokens, "
                     f"{reply.get('seconds')}s, finish={reply.get('finish_reason')}, {findings} findings")
            if record.get("truncated"):
                log.warning(f"{where}: the reply hit the output limit ({reply.get('completion_tokens')} tokens) "
                            f"and was cut off; parsed as {status}")
            elif status != "ok":
                log.warning(f"{where}: the reply is {status} (it counts as a review with no findings): "
                            f"{(reply.get('text') or '')[:200]!r}")
        if due or count == self.calls:
            self.summary()

    def summary(self) -> None:
        with self.lock:
            elapsed = time.monotonic() - self.started
            rate = self.done_tokens / elapsed if elapsed > 0 else 0
            left = (self.tokens - self.done_tokens) / rate if rate > 0 else None
            log.info(f"progress: {self.done}/{self.calls} calls ({100 * self.done / max(self.calls, 1):.1f}%), "
                     f"{self.done_tokens:,} of {self.tokens:,} input tokens, {self.output_tokens:,} output tokens, "
                     f"{rate:,.0f} input tokens/s, elapsed {_duration(elapsed)}, "
                     f"about {_duration(left) if left is not None else '?'} left; statuses {dict(sorted(self.statuses.items()))}, "
                     f"{self.truncated} cut off at the output limit")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _key(data) -> str:
    return sha256_text(json.dumps(data, sort_keys=True, default=str))


def read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_json(path: Path, data) -> None:
    """Atomic write: a crash never leaves a half-written record that looks complete."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


@dataclass(frozen=True)
class Cell:
    entry: str
    level: str
    text: str
    input_tokens: int | None
    known_paths: tuple[str, ...] = ()  # files named in the trace, to make review paths repo-relative


def discover_cells(contexts_path: Path, entries=None, levels=None) -> list[Cell]:
    """Every (entry, level) of the successfully built PRs in the context-level dataset file."""
    from src.tracer_context.dataset import load_records

    cells = []
    for entry_id, record in sorted(load_records(contexts_path).items(), key=lambda kv: (kv[1].get("index", 0), kv[0])):
        if entries and entry_id not in entries:
            continue
        for name, level in record["levels"].items():
            if levels and name not in levels:
                continue
            cells.append(Cell(entry_id, name, level["text"], level.get("input_tokens"),
                              tuple(record.get("known_paths", ()))))
    return cells


def run_reviews(cells: list[Cell], reviewer: Callable[[list[dict]], dict], rubric: str, out_dir: Path,
                max_input_tokens: int | None = None, concurrency: int = 1, force: bool = False,
                progress: Callable | None = None) -> list[dict]:
    """Review each cell once; reuse records whose cache key matches. Cells with the same input
    (a level identical to its parent) are reviewed once and the record is copied to the others
    with `reused_from`. Returns all records, in cell order."""
    out_dir, prompt_hash = Path(out_dir), prompt_sha256(rubric)

    def key_of(cell: Cell) -> str:
        return _key({"input": sha256_text(cell.text), "prompt": prompt_hash, "prompt_version": REVIEW_PROMPT_VERSION,
                     "backend": reviewer.identity, "parser": PARSER_VERSION})

    def path_of(cell: Cell) -> Path:
        return out_dir / "reviews" / cell.entry / f"{cell.level}.json"

    def reusable(cell: Cell, key: str) -> dict | None:
        # Errors are retried; too_long is re-checked against this run's limit (no model call),
        # so raising max_input_tokens later reviews the cells that now fit.
        saved = read_json(path_of(cell))
        if not force and saved and saved.get("key") == key and saved.get("status") not in ("error", "too_long"):
            return saved
        return None

    def too_long(cell: Cell) -> bool:
        return max_input_tokens is not None and (cell.input_tokens or 0) > max_input_tokens

    def one(cell: Cell) -> dict:
        context, key = cell.text, key_of(cell)
        saved = reusable(cell, key)
        if saved:
            return saved
        record = {"key": key, "entry": cell.entry, "level": cell.level, "input_sha256": sha256_text(context),
                  "input_tokens": cell.input_tokens, "prompt_sha256": prompt_hash,
                  "prompt_version": REVIEW_PROMPT_VERSION, "backend": reviewer.identity, "parser": PARSER_VERSION}
        if too_long(cell):
            record.update(status="too_long", error=f"{cell.input_tokens} input tokens > {max_input_tokens}")
            log.info(f"{cell.entry} {cell.level}: too long, not sent ({record['error']})")
        else:
            record["started_at"] = _now()
            log.debug(f"sending {cell.entry} {cell.level} ({cell.input_tokens} input tokens)")
            try:
                reply = reviewer(review_messages(context, rubric))
                parsed = parse_review(reply["text"], cell.known_paths)
                record.update(status=parsed["status"], reply=reply, parsed=parsed,
                              truncated=reply.get("finish_reason") == "length")
            except Exception as error:  # noqa: BLE001 - recorded; retried on the next run
                record.update(status="error", error=f"{type(error).__name__}: {error}"[:2000])
            record["finished_at"] = _now()
            tracker.record(record)
        write_json(path_of(cell), record)
        return record

    keys = [key_of(cell) for cell in cells]
    first: dict[str, Cell] = {}
    for cell, key in zip(cells, keys):
        first.setdefault(key, cell)
    saved_already = sum(1 for key, cell in first.items() if reusable(cell, key))
    to_send = [cell for key, cell in first.items() if not too_long(cell) and not reusable(cell, key)]
    tracker = _ReviewProgress(len(to_send), sum(cell.input_tokens or 0 for cell in to_send))
    log.info(f"review stage: {len(cells)} cells from {len({c.entry for c in cells})} entries, {len(first)} distinct inputs; "
             f"{saved_already} already saved, {sum(too_long(c) for c in first.values())} over the limit of "
             f"{max_input_tokens} input tokens, {len(to_send)} to send ({tracker.tokens:,} input tokens) with "
             f"concurrency {max(1, concurrency)}; reviewer {reviewer.identity}; output {out_dir}")
    done = {}
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for key, record in zip(first, pool.map(one, first.values())):
            done[key] = record
            if progress:
                progress(record)
    results = []
    for cell, key in zip(cells, keys):
        record = done[key]
        if (record["entry"], record["level"]) != (cell.entry, cell.level):  # same input: copy the review
            source = record["level"] if record["entry"] == cell.entry else f"{record['entry']}/{record['level']}"
            record = {**record, "entry": cell.entry, "level": cell.level, "input_tokens": cell.input_tokens,
                      "reused_from": source}
            write_json(out_dir / "reviews" / cell.entry / f"{cell.level}.json", record)
            if progress:
                progress(record)
        results.append(record)
    statuses: dict[str, int] = {}
    for record in results:
        statuses[record["status"]] = statuses.get(record["status"], 0) + 1
    log.info(f"review stage finished: {len(results)} cells, statuses {dict(sorted(statuses.items()))}, "
             f"{tracker.done} model calls in {_duration(time.monotonic() - tracker.started)}"
             + (f"; {statuses['error']} failed and will be retried by the next run" if statuses.get("error") else ""))
    return results


def references_by_entry(dataset_path: Path) -> dict[str, dict]:
    from src.tracer_context.aacr import load_entries

    return {e["id"]: e["record"] for e in load_entries(dataset_path)}


def run_judgments(reviews: list[dict], references: dict[str, dict], judge: Callable[[str], str], out_dir: Path,
                  force: bool = False, progress: Callable | None = None) -> list[dict]:
    """Score each reviewed cell with AACR's metric; reuse records whose cache key matches.

    Malformed and empty replies are scored as reviews with no findings. Cells without a
    reply (too_long, error) are not scored. Judge errors are kept and retried next run.
    """
    out_dir, results = Path(out_dir), []
    judged: dict[str, dict] = {}  # this run's judgments by key: identical reviews are judged once
    scorable = [r for r in reviews if r.get("status") in ("ok", "malformed", "empty_output")]
    log.info(f"judge stage: {len(scorable)} of {len(reviews)} cells have a review to score; "
             f"judge {getattr(judge, 'identity', repr(judge))}; output {out_dir}")
    stage_started, new_calls, errors = time.monotonic(), 0, 0
    for review in reviews:
        if review.get("status") not in ("ok", "malformed", "empty_output"):
            continue
        comments = references[review["entry"]]["comments"]
        key = _key({"review": review["key"], "references": _key(comments),
                    "judge": getattr(judge, "identity", repr(judge)), "judge_prompt": JUDGE_PROMPT_VERSION})
        path = out_dir / "judgments" / review["entry"] / f"{review['level']}.json"
        if key in judged:
            record = {**judged[key], "entry": review["entry"], "level": review["level"],
                      "reused_from": judged[key]["level"]}
            write_json(path, record)
            results.append(record)
            if progress:
                progress(record)
            continue
        saved = read_json(path)
        if not force and saved and saved.get("key") == key and not saved.get("judge_errors"):
            judged[key] = saved
            results.append(saved)
            if progress:
                progress(saved)
            continue
        started = time.monotonic()
        findings = review["parsed"]["findings"] if review.get("parsed") else []
        score = score_review(comments, findings, judge)
        record = {"key": key, "entry": review["entry"], "level": review["level"], "review_key": review["key"],
                  "judge": getattr(judge, "identity", repr(judge)), "judge_prompt_version": JUDGE_PROMPT_VERSION,
                  "seconds": round(time.monotonic() - started, 2), **score}
        write_json(path, record)
        judged[key] = record
        results.append(record)
        new_calls += 1
        errors += record["judge_errors"]
        level = logging.WARNING if record["judge_errors"] else logging.INFO
        log.log(level, f"judged {len(results)}/{len(scorable)} {record['entry']} {record['level']}: "
                       f"{record['generated']} generated, {record['expected']} expected, {record['covered']} covered, "
                       f"recall {record['recall']:.2f}, {record['judge_errors']} judge errors, {record['seconds']}s")
        if progress:
            progress(record)
    log.info(f"judge stage finished: {len(results)} cells scored, {new_calls} judged now, "
             f"{len(results) - new_calls} reused, {errors} judge errors"
             + (" (re-run to retry them)" if errors else "") + f", in {_duration(time.monotonic() - stage_started)}")
    return results


def _split(comments, covered_indices, keep) -> tuple[int, int]:
    indices = [i for i, c in enumerate(comments) if c.get("note") and keep(c)]
    return len(indices), sum(1 for i in indices if i in covered_indices)


def build_table(reviews: list[dict], judgments: list[dict], references: dict[str, dict]) -> tuple[list, list]:
    """(rows, coverage): one row per (entry, level), and one per (entry, level, reference)."""
    judged = {(j["entry"], j["level"]): j for j in judgments}
    rows, coverage = [], []
    for review in sorted(reviews, key=lambda r: (r["entry"], r["level"])):
        record = references[review["entry"]]
        comments = record["comments"]
        judgment = judged.get((review["entry"], review["level"]))
        reply, parsed = review.get("reply") or {}, review.get("parsed") or {}
        row = {"entry": review["entry"], "level": review["level"], "language": record.get("project_main_language"),
               "input_tokens": review.get("input_tokens"), "review_status": review.get("status"),
               "truncated": review.get("truncated"), "findings": len(parsed.get("findings", [])),
               "invalid_findings": parsed.get("invalid_findings"), "output_tokens": reply.get("completion_tokens"),
               "review_seconds": reply.get("seconds"), "reused_from": review.get("reused_from")}
        if judgment:
            covered = set(judgment["covered_indices"])
            human = _split(comments, covered, lambda c: not c.get("is_ai_comment"))
            ai = _split(comments, covered, lambda c: c.get("is_ai_comment"))
            row.update(expected=judgment["expected"], generated=judgment["generated"],
                       matched_generated=judgment["matched_generated"], covered=judgment["covered"],
                       recall=round(judgment["recall"], 4), precision=round(judgment["precision"], 4),
                       balanced=round(judgment["balanced"], 4), human_expected=human[0], human_covered=human[1],
                       ai_expected=ai[0], ai_covered=ai[1], judge_errors=judgment["judge_errors"])
            for index, comment in enumerate(comments):
                if comment.get("note"):
                    coverage.append({"entry": review["entry"], "level": review["level"], "reference": index,
                                     "covered": index in covered, "is_ai_comment": comment.get("is_ai_comment"),
                                     "source_model": comment.get("source_model") or "", "context": comment.get("context"),
                                     "category": comment.get("category"), "path": comment.get("path")})
        rows.append(row)
    return rows, coverage


def summarize_by_level(rows: list[dict]) -> list[dict]:
    """Per level: pooled recall/precision/balanced over judged PRs (AACR's way) plus the
    per-PR average recall, mean input tokens and output-status counts."""
    levels: dict[str, list[dict]] = {}
    for row in rows:
        levels.setdefault(row["level"], []).append(row)
    summary = []
    for level in sorted(levels):
        group, judged = levels[level], [r for r in levels[level] if "recall" in r]
        expected = sum(r["expected"] for r in judged)
        generated = sum(r["generated"] for r in judged)
        recall = sum(r["covered"] for r in judged) / expected if expected else 0.0
        precision = sum(r["matched_generated"] for r in judged) / generated if generated else 0.0
        human_expected = sum(r["human_expected"] for r in judged)
        tokens = [r["input_tokens"] for r in group if r.get("input_tokens") is not None]
        statuses: dict[str, int] = {}
        for r in group:
            statuses[r["review_status"]] = statuses.get(r["review_status"], 0) + 1
        summary.append({
            "level": level, "cells": len(group), "judged": len(judged),
            "mean_input_tokens": round(sum(tokens) / len(tokens)) if tokens else None,
            "recall_pooled": round(recall, 4), "precision_pooled": round(precision, 4),
            "balanced_pooled": round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0,
            "recall_mean_per_pr": round(sum(r["recall"] for r in judged) / len(judged), 4) if judged else None,
            "human_recall_pooled": round(sum(r["human_covered"] for r in judged) / human_expected, 4)
            if human_expected else None,
            "statuses": statuses})
    return summary


def write_outputs(out_dir: Path, rows: list[dict], coverage: list[dict]) -> None:
    out_dir = Path(out_dir)
    for name, data in (("table", rows), ("coverage", coverage)):
        if not data:
            continue
        fields = sorted({k for row in data for k in row}, key=lambda k: list(data[0]).index(k)
                        if k in data[0] else len(data[0]))
        with (out_dir / f"{name}.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=fields)
            writer.writeheader()
            writer.writerows(data)
    write_json(out_dir / "table.json", rows)  # one JSON array, a row per (entry, level)
    (out_dir / "table.jsonl").unlink(missing_ok=True)  # the older JSON Lines copy would be stale
