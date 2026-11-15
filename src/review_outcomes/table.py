"""Build the offline (PR x level) outcome table from the context-level dataset file
(`<root>/context_levels.jsonl`, e.g. data/aacr/, written by scripts/dataset/build_dataset.py).

Layout under `out_dir` (one folder per run, e.g. data/aacr/outcomes/<run_name>/):
  manifest.json                         run settings, prompt and dataset hashes
  reviews/<entry>/<level>.json          prompt hashes, backend identity, raw reply, parsed findings
                                        (and every reply of a retried cell, under "retry": see retry.py)
  judgments/<entry>/<level>.json        AACR judge records per generated comment, per-reference flags
  table.csv / table.json                one row per (entry, level); table.json is one JSON array
  coverage.csv                          one row per (entry, level, reference): covered or not
  logs/<stage>_<time>.log               the run's log, when start_run_log() was called (the notebook does)

Every record carries a cache key over all behavior-affecting inputs, so re-running resumes:
matching cells are reused, changed ones are redone. Reference comments are read only in the
judging stage; they never reach the reviewer.

A per-hunk run (a context file built with `--unit hunk`) has one cell per (diff hunk, level): its
entry is the hunk's id (`<PR id>__h0001`) and the layout above is the same. Its table is built by
`build_hunk_tables`: table.csv / table.json have one row per (PR, level), the PR's hunk reviews
at that level put together and scored as one review; hunk_table.csv has one row per (hunk, level).
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

from src.review_outcomes import retry as retry_rule
from src.review_outcomes.judge import normalize_separators, score_review
from src.review_outcomes.parse import PARSER_VERSION, TAG_PARSER_VERSION, parse_review, parse_tagged_review
from src.review_outcomes.prompts import JUDGE_PROMPT_VERSION, ReviewInput, ReviewPrompt, as_prompt
from src.review_outcomes.retry import RetryPolicy


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
                     f"{reply.get('seconds')}s, finish={reply.get('finish_reason')}, {findings} findings"
                     + (f" (after {retry_rule.steps_taken(record)})" if retry_rule.steps_taken(record) else ""))
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
    # A per-hunk cell only: its PR, the file under review, and the hunk's lines
    # (new_start, new_end, old_start, old_end).
    pr: str | None = None
    file: str | None = None
    span: tuple[int, int, int, int] | None = None
    # The input in parts, for a prompt with a slot for each; None for a context file whose levels
    # do not record where the parts are (one built before the per-hunk test).
    parts: ReviewInput | None = None

    @property
    def group(self) -> str:
        """The PR this cell belongs to (its entry, unless it is one hunk of a PR)."""
        return self.pr or self.entry


def discover_cells(contexts_path: Path, entries=None, levels=None) -> list[Cell]:
    """Every (entry, level) of the successfully built PRs in the context-level dataset file. For
    a per-hunk file the entries are the hunks; `entries` may then name PRs or hunks."""
    from src.tracer_context.build import split_input
    from src.tracer_context.dataset import hunk_units, load_records

    cells = []
    for entry_id, record in sorted(load_records(contexts_path).items(), key=lambda kv: (kv[1].get("index", 0), kv[0])):
        for unit in (hunk_units([record]) if "hunks" in record else [record]):
            if entries and unit["id"] not in entries and entry_id not in entries:
                continue
            hunk = unit.get("hunk")
            extra = {} if hunk is None else {
                "pr": entry_id, "file": hunk["file"],
                "span": (hunk["new_start"], hunk["new_start"] + max(hunk["new_count"], 1) - 1,
                         hunk["old_start"], hunk["old_start"] + max(hunk["old_count"], 1) - 1)}
            for name, level in unit["levels"].items():
                if levels and name not in levels:
                    continue
                parts = split_input(level["text"], level["sections"]) if level.get("sections") else None
                cells.append(Cell(unit["id"], name, level["text"], level.get("input_tokens"),
                                  tuple(unit.get("known_paths", ())), **extra,
                                  parts=None if parts is None else ReviewInput(**parts)))
    return cells


class _ReviewFiles:
    """Where each cell's review is saved, its cache key, and the saved record a run can build on."""

    def __init__(self, reviewer, rubric: str | ReviewPrompt, out_dir: Path, max_input_tokens: int | None, force: bool,
                 retry: RetryPolicy | None):
        self.reviewer, self.out_dir, self.max_input_tokens = reviewer, Path(out_dir), max_input_tokens
        self.force, self.retry = force, retry
        self.prompt = as_prompt(rubric)  # a rubric text means the Codex prompt
        self.prompt_hash = self.prompt.sha256

    def parser_version(self, cell: Cell) -> str:
        if self.prompt.reply_format == "aacr-tags":
            return TAG_PARSER_VERSION
        # A per-hunk cell's findings are placed in the hunk's file (parse_review's `path`).
        return PARSER_VERSION if cell.file is None else f"{PARSER_VERSION}+hunk-path"

    def key(self, cell: Cell) -> str:
        # The first call only. The retry policy is not in it, so adding or changing the policy keeps
        # every cell whose first reply needs no retry; a retried record names its policy itself.
        return _key({"input": sha256_text(cell.text), "prompt": self.prompt_hash, "prompt_version": self.prompt.version,
                     "backend": self.reviewer.identity, "parser": self.parser_version(cell)})

    def path(self, cell: Cell) -> Path:
        return self.out_dir / "reviews" / cell.entry / f"{cell.level}.json"

    def too_long(self, cell: Cell) -> bool:
        return self.max_input_tokens is not None and (cell.input_tokens or 0) > self.max_input_tokens

    def parser(self, cell: Cell) -> Callable[[str], dict]:
        if self.prompt.reply_format == "aacr-tags":  # the paper's tags: lines come from the quoted snippet
            diff = cell.parts.diff if cell.parts else None
            return lambda text: parse_tagged_review(text, path=cell.file, diff=diff)
        return lambda text: parse_review(text, cell.known_paths, path=cell.file)

    def load(self, cell: Cell, key: str) -> tuple[dict | None, bool]:
        """(the saved record to build on, whether it differs from the file), or (None, False).

        Errors are retried; too_long is re-checked against this run's limit (no model call), so
        raising max_input_tokens later reviews the cells that now fit. Retries made under another
        retry policy are undone, which leaves the first reply for this run's policy to act on.
        """
        saved = read_json(self.path(cell))
        if self.force or not saved or saved.get("key") != key or saved.get("status") in ("error", "too_long"):
            return None, False
        record = retry_rule.align(saved, self.retry, self.parser(cell))
        return record, record is not saved


def _plan(files: _ReviewFiles, cells: list[Cell]) -> tuple[list[str], dict[str, Cell], dict]:
    """Each cell's key, the first cell of every distinct input, and what a run has to do."""
    keys = [files.key(cell) for cell in cells]
    first: dict[str, Cell] = {}
    for cell, key in zip(cells, keys):
        first.setdefault(key, cell)
    send, saved, no_room = [], 0, []
    due: dict[str, list[Cell]] = {retry_rule.LONGER_OUTPUT: [], retry_rule.FOLLOW_UP: []}
    first_limit = getattr(files.reviewer, "max_tokens", None)
    for key, cell in first.items():
        record, _ = files.load(cell, key)
        if record is None:
            if not files.too_long(cell):
                send.append(cell)
            continue
        saved += 1
        step = retry_rule.next_step(record, files.retry)
        if step:
            _, why = retry_rule.step_limit(step, record, files.retry, first_limit)
            (no_room if why else due[step]).append(cell)
    tokens = lambda group: sum(cell.input_tokens or 0 for cell in group)  # noqa: E731
    return keys, first, {
        "cells": len(cells), "entries": len({c.entry for c in cells}), "distinct": len(first), "saved": saved,
        "prs": len({c.group for c in cells}), "hunks": len({c.entry for c in cells if c.pr is not None}),
        "too_long": sum(files.too_long(c) for c in first.values()),
        "send": {"cells": len(send), "input_tokens": tokens(send)},
        "retry": {step: {"cells": len(group), "input_tokens": tokens(group)} for step, group in due.items()},
        "no_room": len(no_room)}


def plan_reviews(cells: list[Cell], reviewer, rubric: str | ReviewPrompt, out_dir: Path,
                 max_input_tokens: int | None = None, force: bool = False, retry: RetryPolicy | None = None) -> dict:
    """What run_reviews would do with these arguments, without calling the model or writing a file:
    the cells to send, the saved replies due each retry step (with their input tokens), and the
    replies due a step that has no room in the model's window."""
    return _plan(_ReviewFiles(reviewer, rubric, out_dir, max_input_tokens, force, retry), cells)[2]


def run_reviews(cells: list[Cell], reviewer: Callable[[list[dict]], dict], rubric: str | ReviewPrompt, out_dir: Path,
                max_input_tokens: int | None = None, concurrency: int = 1, force: bool = False,
                progress: Callable | None = None, retry: RetryPolicy | None = None) -> list[dict]:
    """Review each cell once; reuse records whose cache key matches. `rubric` is the Codex rubric
    text or a ReviewPrompt (prompts.load_review_prompt). Cells with the same input
    (a level identical to its parent) are reviewed once and the record is copied to the others
    with `reused_from`. With a `retry` policy, a reply that was cut off or finished without the
    JSON gets the policy's steps (retry.py), whether it arrives in this run or was saved by an
    earlier one. Returns all records, in cell order."""
    out_dir = Path(out_dir)
    files = _ReviewFiles(reviewer, rubric, out_dir, max_input_tokens, force, retry)

    def report_step(entry: dict, record: dict) -> None:
        where = f"retry {record['entry']} {record['level']}: {entry['step']}"
        if "skipped" in entry:
            log.warning(f"{where} skipped: {entry['skipped']}")
        elif "error" in entry:
            log.error(f"{where} FAILED: {entry['error']} (the first reply stays; the next run tries the step again)")
        else:
            reply = entry["reply"]
            log.info(f"{where} with an output limit of {entry['max_tokens']}: {entry['status']}, "
                     f"{reply.get('completion_tokens')} output tokens, {reply.get('seconds')}s, "
                     f"finish={reply.get('finish_reason')}, {len(record['parsed']['findings'])} findings")

    def one(cell: Cell) -> dict:
        key = files.key(cell)
        record, changed = files.load(cell, key)
        called = False
        if record is None:
            context = cell.text
            record = {"key": key, "entry": cell.entry, "level": cell.level, "input_sha256": sha256_text(context),
                      "input_tokens": cell.input_tokens, "prompt_sha256": files.prompt_hash,
                      "prompt_version": files.prompt.version, "backend": reviewer.identity,
                      "parser": files.parser_version(cell),
                      **({} if cell.pr is None else {"pr": cell.pr, "file": cell.file})}
            if files.too_long(cell):
                record.update(status="too_long", error=f"{cell.input_tokens} input tokens > {max_input_tokens}")
                log.info(f"{cell.entry} {cell.level}: too long, not sent ({record['error']})")
                write_json(files.path(cell), record)
                return record
            record["started_at"] = _now()
            log.debug(f"sending {cell.entry} {cell.level} ({cell.input_tokens} input tokens)")
            try:
                reply = reviewer(files.prompt.messages(context, cell.parts))
                parsed = files.parser(cell)(reply["text"])
                record.update(status=parsed["status"], reply=reply, parsed=parsed,
                              truncated=reply.get("finish_reason") == "length")
            except Exception as error:  # noqa: BLE001 - recorded; retried on the next run
                record.update(status="error", error=f"{type(error).__name__}: {error}"[:2000])
            record["finished_at"] = _now()
            called = changed = True
        step = retry_rule.next_step(record, retry)
        if step:
            sends = retry_rule.step_limit(step, record, retry, getattr(reviewer, "max_tokens", None))[1] is None
            if called and sends:  # the first reply arrived in this run: say why another call follows
                log.info(f"{cell.entry} {cell.level}: the first reply "
                         + ("was cut off at the output limit" if record.get("truncated") else "finished without the JSON")
                         + f" ({record['reply'].get('completion_tokens')} output tokens); retrying")
            record = retry_rule.run_steps(record, files.prompt.messages(cell.text, cell.parts), reviewer, retry,
                                          files.parser(cell), report_step)
            called, changed = called or sends, True
        if called:
            tracker.record(record)
        if changed:
            write_json(files.path(cell), record)
        return record

    keys, first, plan = _plan(files, cells)
    due = plan["retry"]
    tracker = _ReviewProgress(plan["send"]["cells"] + sum(group["cells"] for group in due.values()),
                              plan["send"]["input_tokens"] + sum(group["input_tokens"] for group in due.values()))
    retry_note = "" if retry is None else (
        f"; {sum(group['cells'] for group in due.values())} saved replies to retry "
        f"({due[retry_rule.LONGER_OUTPUT]['cells']} cut off, {due[retry_rule.FOLLOW_UP]['cells']} finished without the "
        f"JSON; {plan['no_room']} more have no room in the window); retry policy {retry.identity}")
    log.info(f"review stage: {len(cells)} cells from {plan['entries']} entries, {len(first)} distinct inputs; "
             f"{plan['saved']} already saved, {plan['too_long']} over the limit of "
             f"{max_input_tokens} input tokens, {plan['send']['cells']} to send ({plan['send']['input_tokens']:,} input "
             f"tokens) with concurrency {max(1, concurrency)}{retry_note}; reviewer {reviewer.identity}; "
             f"prompt {files.prompt.name} ({files.prompt.version}); output {out_dir}")
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
    if retry is not None:
        log.info(_retry_summary(list(done.values())))
    return results


def _retry_summary(records: list[dict]) -> str:
    """One line on where the retry rule stands for these distinct replies."""
    retried = [r for r in records if r.get("retry")]
    steps = [step for r in retried for step in r["retry"]["steps"]]
    count = lambda name, field: sum(1 for s in steps if s["step"] == name and field in s)  # noqa: E731
    answered = sum(1 for r in retried if retry_rule.steps_taken(r))
    return (f"retry rule: {len(retried)} of {len(records)} distinct replies took a retry step, {answered} got a new "
            f"reply; of those {len(retried)}, {sum(r['status'] == 'ok' for r in retried)} now have an answer and "
            f"{sum(r['status'] != 'ok' for r in retried)} do not ({sum(bool(r.get('truncated')) for r in retried)} "
            f"still cut off). Steps: {count(retry_rule.LONGER_OUTPUT, 'reply')} longer output, "
            f"{count(retry_rule.FOLLOW_UP, 'reply')} follow-up, "
            f"{sum(1 for s in steps if 'skipped' in s)} skipped for lack of room, {sum(1 for s in steps if 'error' in s)} failed")


def references_by_entry(dataset_path: Path, cells: list[Cell] | None = None) -> dict[str, dict]:
    """Each PR's AACR record by entry id. With per-hunk `cells`, each hunk's id also maps to its
    PR's record: a hunk's findings are judged against the PR's references on the same file."""
    from src.tracer_context.aacr import load_entries

    references = {e["id"]: e["record"] for e in load_entries(dataset_path)}
    for cell in cells or ():
        if cell.pr is not None:
            references[cell.entry] = references[cell.pr]
    return references


def run_judgments(reviews: list[dict], references: dict[str, dict], judge: Callable[[str], str], out_dir: Path,
                  force: bool = False, progress: Callable | None = None, concurrency: int = 1) -> list[dict]:
    """Score each reviewed cell with AACR's metric; reuse records whose cache key matches.

    Malformed and empty replies are scored as reviews with no findings. Cells without a
    reply (too_long, error) are not scored. Judge errors are kept and retried next run.
    Identical reviews are judged once; `concurrency` reviews are judged at a time.
    """
    out_dir = Path(out_dir)
    identity = getattr(judge, "identity", repr(judge))
    scorable = []  # (review, key, path), in order
    for review in reviews:
        if review.get("status") not in ("ok", "malformed", "empty_output"):
            continue
        comments = references[review["entry"]]["comments"]
        # A retried review's key covers its first call only, so its answer in force is named too.
        answer = retry_rule.answer_id(review)
        key = _key({"review": review["key"], **({"answer": answer} if answer else {}), "references": _key(comments),
                    "judge": identity, "judge_prompt": JUDGE_PROMPT_VERSION})
        scorable.append((review, key, out_dir / "judgments" / review["entry"] / f"{review['level']}.json"))
    judged: dict[str, dict] = {}  # by key: the saved judgment, or the one made in this run
    todo: dict[str, tuple[dict, Path]] = {}
    for review, key, path in scorable:
        if key in judged or key in todo:
            continue
        saved = read_json(path)
        if not force and saved and saved.get("key") == key and not saved.get("judge_errors"):
            judged[key] = saved
        else:
            todo[key] = (review, path)
    log.info(f"judge stage: {len(scorable)} of {len(reviews)} cells have a review to score; {len(judged)} distinct "
             f"reviews already judged, {len(todo)} to judge with concurrency {max(1, concurrency)}; "
             f"judge {identity}; output {out_dir}")
    stage_started, counter, lock = time.monotonic(), {"done": 0, "errors": 0}, threading.Lock()

    def judge_one(item: tuple[str, tuple[dict, Path]]) -> dict:
        key, (review, path) = item
        started = time.monotonic()
        findings = review["parsed"]["findings"] if review.get("parsed") else []
        score = score_review(references[review["entry"]]["comments"], findings, judge)
        record = {"key": key, "entry": review["entry"], "level": review["level"], "review_key": review["key"],
                  "judge": identity, "judge_prompt_version": JUDGE_PROMPT_VERSION,
                  "seconds": round(time.monotonic() - started, 2), **score}
        write_json(path, record)
        with lock:
            counter["done"] += 1
            counter["errors"] += record["judge_errors"]
            count = counter["done"]
        level = logging.WARNING if record["judge_errors"] else logging.INFO
        log.log(level, f"judged {count}/{len(todo)} {record['entry']} {record['level']}: "
                       f"{record['generated']} generated, {record['expected']} expected, {record['covered']} covered, "
                       f"recall {record['recall']:.2f}, {record['judge_errors']} judge errors, {record['seconds']}s")
        return record

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for key, record in zip(todo, pool.map(judge_one, todo.items())):
            judged[key] = record
    results, placed = [], set()
    for review, key, path in scorable:
        record = judged[key]
        if key in placed:  # an identical review: copy its judgment
            record = {**record, "entry": review["entry"], "level": review["level"], "reused_from": judged[key]["level"]}
            write_json(path, record)
        placed.add(key)
        results.append(record)
        if progress:
            progress(record)
    log.info(f"judge stage finished: {len(results)} cells scored, {len(todo)} judged now, "
             f"{len(results) - len(todo)} reused, {counter['errors']} judge errors"
             + (" (re-run to retry them)" if counter["errors"] else "") + f", in {_duration(time.monotonic() - stage_started)}")
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
               "truncated": review.get("truncated"), "retry": retry_rule.steps_taken(review),
               "findings": len(parsed.get("findings", [])),
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


def _in_hunk(comment: dict, cell: Cell) -> bool:
    """Whether a reference comment sits on this hunk: the same file, and its lines overlap the
    hunk's lines on the comment's side."""
    if normalize_separators(comment.get("path")) != cell.file or cell.span is None:
        return False
    start, end = comment.get("from_line"), comment.get("to_line")
    if not start or not end:
        return False
    low, high = cell.span[2:] if comment.get("side") == "left" else cell.span[:2]
    return start <= high and end >= low


def build_hunk_tables(cells: list[Cell], reviews: list[dict], judgments: list[dict],
                      references: dict[str, dict]) -> tuple[list, list, list]:
    """(PR rows, hunk rows, coverage) of a per-hunk run.

    PR rows: one per (PR, level), with build_table's columns. The PR's hunk reviews at that level
    are put together in diff order and scored as one review by AACR's rule: a finding counts as
    matched when it covers a reference that no earlier finding covered. `hunks`, `hunks_reviewed`
    and `hunks_ok` say how many of the PR's hunks have a reply and a readable one;
    `review_status` is "ok" only when all of them do.
    Hunk rows: one per (hunk, level): the hunk's own findings, and the references that sit on it.
    Coverage: one row per (PR, level, reference), as build_table writes it."""
    reviewed = {(r["entry"], r["level"]): r for r in reviews}
    judged = {(j["entry"], j["level"]): j for j in judgments}
    groups: dict[tuple[str, str], list[Cell]] = {}
    for cell in cells:
        if cell.pr is None:
            raise ValueError(f"{cell.entry} is not a per-hunk cell; use build_table")
        groups.setdefault((cell.pr, cell.level), []).append(cell)
    pr_rows, hunk_rows, coverage = [], [], []
    for (pr, level), group in sorted(groups.items()):
        record = references[pr]
        comments = record["comments"]
        covered, matched_generated, generated, findings, judge_errors, scored = set(), 0, 0, 0, 0, 0
        statuses: dict[str, int] = {}
        tokens = [c.input_tokens for c in group if c.input_tokens is not None]
        output_tokens = seconds = 0
        for cell in group:
            review = reviewed.get((cell.entry, level))
            judgment = judged.get((cell.entry, level))
            status = review.get("status") if review else "missing"
            statuses[status] = statuses.get(status, 0) + 1
            reply, parsed = (review or {}).get("reply") or {}, (review or {}).get("parsed") or {}
            output_tokens += reply.get("completion_tokens") or 0
            seconds += reply.get("seconds") or 0
            findings += len(parsed.get("findings", []))
            on_hunk = {i for i, c in enumerate(comments) if c.get("note") and _in_hunk(c, cell)}
            hunk_row = {"entry": cell.entry, "pr": pr, "level": level, "file": cell.file,
                        "new_start": cell.span[0], "new_end": cell.span[1], "input_tokens": cell.input_tokens,
                        "review_status": status, "truncated": (review or {}).get("truncated"),
                        "retry": retry_rule.steps_taken(review) if review else None,
                        "findings": len(parsed.get("findings", [])), "output_tokens": reply.get("completion_tokens"),
                        "review_seconds": reply.get("seconds"), "reused_from": (review or {}).get("reused_from"),
                        "references_on_hunk": len(on_hunk)}
            if judgment:
                scored += 1
                generated += judgment["generated"]
                judge_errors += judgment["judge_errors"]
                own = set()
                for item in judgment["records"]:
                    hit = set(item["matched_gt_indices"])
                    own |= hit
                    if hit - covered:
                        matched_generated += 1
                        covered |= hit
                hunk_row.update(findings_matched=sum(1 for item in judgment["records"] if item["matched_gt_indices"]),
                                references_covered=len(own), references_on_hunk_covered=len(own & on_hunk),
                                judge_errors=judgment["judge_errors"])
            hunk_rows.append(hunk_row)
        ok = statuses.get("ok", 0)
        row = {"entry": pr, "level": level, "language": record.get("project_main_language"),
               "input_tokens": sum(tokens) if tokens else None,
               "review_status": "ok" if ok == len(group) else "incomplete",
               "hunks": len(group), "hunks_reviewed": len(group) - statuses.get("missing", 0), "hunks_ok": ok,
               "hunk_statuses": json.dumps(dict(sorted(statuses.items()))), "findings": findings,
               "output_tokens": output_tokens, "review_seconds": round(seconds, 2)}
        if scored:
            expected = sum(1 for c in comments if c.get("note"))
            recall = len(covered) / expected if expected else 0.0
            precision = matched_generated / generated if generated else 0.0
            human = _split(comments, covered, lambda c: not c.get("is_ai_comment"))
            ai = _split(comments, covered, lambda c: c.get("is_ai_comment"))
            row.update(expected=expected, generated=generated, matched_generated=matched_generated, covered=len(covered),
                       recall=round(recall, 4), precision=round(precision, 4),
                       balanced=round(2 * precision * recall / (precision + recall), 4) if precision + recall else 0.0,
                       human_expected=human[0], human_covered=human[1], ai_expected=ai[0], ai_covered=ai[1],
                       judge_errors=judge_errors)
            for index, comment in enumerate(comments):
                if comment.get("note"):
                    coverage.append({"entry": pr, "level": level, "reference": index, "covered": index in covered,
                                     "is_ai_comment": comment.get("is_ai_comment"),
                                     "source_model": comment.get("source_model") or "", "context": comment.get("context"),
                                     "category": comment.get("category"), "path": comment.get("path")})
        pr_rows.append(row)
    return pr_rows, hunk_rows, coverage


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


def write_outputs(out_dir: Path, rows: list[dict], coverage: list[dict], hunk_rows: list[dict] | None = None) -> None:
    """table.csv, table.json and coverage.csv; with `hunk_rows` (a per-hunk run) also hunk_table.csv."""
    out_dir = Path(out_dir)
    for name, data in (("table", rows), ("coverage", coverage), ("hunk_table", hunk_rows)):
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
