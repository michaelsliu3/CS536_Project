"""Build the offline (PR x level) outcome table from the context-level dataset file
(`<root>/context_levels.jsonl`, e.g. data/aacr/, written by scripts/dataset/build_dataset.py).

Layout under `out_dir` (one folder per run, e.g. data/aacr/outcomes/<run_name>/):
  manifest.json                         run settings, prompt and dataset hashes
  reviews/<entry>/<level>.json          prompt hashes, backend identity, raw reply, parsed findings
  judgments/<entry>/<level>.json        AACR judge records per generated comment, per-reference flags
  table.csv / table.json                one row per (entry, level); table.json is one JSON array
  coverage.csv                          one row per (entry, level, reference): covered or not

Every record carries a cache key over all behavior-affecting inputs, so re-running resumes:
matching cells are reused, changed ones are redone. Reference comments are read only in the
judging stage; they never reach the reviewer.
"""
from __future__ import annotations

import csv
import hashlib
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.review_outcomes.judge import score_review
from src.review_outcomes.parse import PARSER_VERSION, parse_review
from src.review_outcomes.prompts import JUDGE_PROMPT_VERSION, REVIEW_PROMPT_VERSION, prompt_sha256, review_messages


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

    def one(cell: Cell) -> dict:
        context, key = cell.text, key_of(cell)
        path = out_dir / "reviews" / cell.entry / f"{cell.level}.json"
        saved = read_json(path)
        if not force and saved and saved.get("key") == key and saved.get("status") not in ("error",):
            return saved
        record = {"key": key, "entry": cell.entry, "level": cell.level, "input_sha256": sha256_text(context),
                  "input_tokens": cell.input_tokens, "prompt_sha256": prompt_hash,
                  "prompt_version": REVIEW_PROMPT_VERSION, "backend": reviewer.identity, "parser": PARSER_VERSION}
        if max_input_tokens is not None and (cell.input_tokens or 0) > max_input_tokens:
            record.update(status="too_long", error=f"{cell.input_tokens} input tokens > {max_input_tokens}")
        else:
            try:
                reply = reviewer(review_messages(context, rubric))
                parsed = parse_review(reply["text"], cell.known_paths)
                record.update(status=parsed["status"], reply=reply, parsed=parsed,
                              truncated=reply.get("finish_reason") == "length")
            except Exception as error:  # noqa: BLE001 - recorded; retried on the next run
                record.update(status="error", error=f"{type(error).__name__}: {error}"[:2000])
        write_json(path, record)
        return record

    keys = [key_of(cell) for cell in cells]
    first: dict[str, Cell] = {}
    for cell, key in zip(cells, keys):
        first.setdefault(key, cell)
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
        if progress:
            progress(record)
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
