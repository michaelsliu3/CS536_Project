"""Severity labels for reference comments and validity ratings for the reviewer's findings.

Two model jobs behind the review score (docs/review_score_design.md):

- `label_references`: each reference comment gets a tier (blocking, should_fix, nit). The model
  sees the comment and the code it is about, not who wrote the comment.
- `rate_findings`: each finding the judge matched to no reference gets a validity (valid,
  speculative, wrong) and a tier. The model sees the finding and the input the reviewer saw.

Both are made once and saved; a later run skips what is saved under the same prompt, model
settings and input. The model is the caller's choice (a chat backend from backends.py).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from src.review_outcomes.judge import normalize_separators

RUBRIC_VERSION = "severity-v1"
TIERS = ("blocking", "should_fix", "nit")
VALIDITIES = ("valid", "speculative", "wrong")

RUBRIC = """\
Severity tiers, for the problem a comment names if the comment is right:
- blocking: breaks the build or the tests, gives wrong behaviour on a normal path, breaks an API \
or its callers, opens a security hole, or can lose or corrupt data.
- should_fix: a real defect with limited effect: a rare path, missing error handling, a real but \
bounded performance cost, a resource that is not released.
- nit: naming, comment wording, formatting, style, or a refactoring that changes no behaviour."""

LABEL_PROMPT = """\
You are grading how much a code review comment matters. Judge from the code, not from the \
comment's wording: words such as "critical" or "serious" in the comment are not evidence.

{rubric}

## Code under review

{code}

## Review comment

File: {path}
Lines: {lines}

{note}

## Answer

Reply with one JSON object and nothing else:
{{"tier": "blocking" | "should_fix" | "nit", "reason": "<one sentence>", "confidence": <0 to 1>}}"""

RATE_PROMPT = """\
You are checking one comment that a code reviewer wrote about a change. Decide whether the \
comment is right, using only the code shown below, and how much the problem it names matters.

Validity:
- valid: the problem the comment names is real and can be confirmed from the code shown.
- speculative: the comment may be right, but the code shown cannot confirm it. It asks the \
author to check something, or it depends on code that is not shown.
- wrong: the code shown contradicts the comment, or what it names is not a problem.

{rubric}

## What the reviewer was shown

{code}

## The reviewer's comment

File: {path}
Quoted code:
{snippet}

{note}

## Answer

Reply with one JSON object and nothing else:
{{"validity": "valid" | "speculative" | "wrong", "tier": "blocking" | "should_fix" | "nit", \
"reason": "<one sentence>"}}"""

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_OBJECT = re.compile(r"\{[^{}]*\}", re.DOTALL)
_TIER_ALIASES = {"should fix": "should_fix", "should-fix": "should_fix", "shouldfix": "should_fix"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _key(prompt: str, identity) -> str:
    return _sha(json.dumps({"prompt": _sha(prompt), "backend": identity, "rubric": RUBRIC_VERSION}, sort_keys=True))


def parse_answer(text: str, fields: dict[str, tuple[str, ...]]) -> dict | None:
    """The last JSON object in a reply that has every field of `fields` with an allowed value
    ({field: allowed values}), or None. Thinking and any text around the object are ignored."""
    body = _THINK.sub("", text or "")
    if "</think>" in body:
        body = body.split("</think>", 1)[1]
    for raw in reversed(_OBJECT.findall(body)):
        try:
            answer = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(answer, dict):
            continue
        clean = {}
        for field, allowed in fields.items():
            value = str(answer.get(field, "")).strip().lower()
            value = _TIER_ALIASES.get(value, value)
            if value not in allowed:
                break
            clean[field] = value
        else:
            for extra in ("reason", "confidence"):
                if extra in answer:
                    clean[extra] = answer[extra]
            return clean
    return None


def _run(items: list[dict], caller: Callable[[list[dict]], dict], fields: dict, concurrency: int,
         done: Callable[[dict], None]) -> None:
    """Send each item's prompt and hand its result to `done`: the item plus the parsed fields, or
    plus `error`. A reply that cannot be read is an error, never a guess."""
    def one(item: dict) -> dict:
        result = {k: v for k, v in item.items() if k != "prompt"}
        try:
            reply = caller([{"role": "user", "content": item["prompt"]}])
            answer = parse_answer(reply["text"], fields)
            if answer is None:
                result.update(error="unreadable reply", reply=reply["text"][-2000:])
            else:
                result.update(answer, output_tokens=reply.get("completion_tokens"), seconds=reply.get("seconds"))
        except Exception as error:  # noqa: BLE001 - one failed call must not stop the run
            result["error"] = f"{type(error).__name__}: {error}"
        return result

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for result in pool.map(one, items):
            done(result)


# ---------- reference comments ----------

def code_for_reference(comment: dict, cells: list) -> tuple[str, str | None]:
    """(the code to show for a reference comment, the cell it came from or None).

    `cells` are one PR's per-hunk cells at one level. The hunk the comment sits on is preferred
    (same file, overlapping lines on the comment's side); otherwise the file's first hunk. A
    comment on a file with no hunk gets no code."""
    from src.review_outcomes.table import _in_hunk

    path = normalize_separators(comment.get("path"))
    same_file = [c for c in cells if c.file == path]
    on_hunk = [c for c in same_file if _in_hunk(comment, c)]
    chosen = (on_hunk or same_file or [None])[0]
    return (chosen.text, chosen.entry) if chosen else ("(the code of this file is not available)", None)


def reference_items(references: dict[str, dict], cells: list, identity) -> list[dict]:
    """One item per reference comment of the PRs in `cells`: {pr, reference, prompt, key, ...}."""
    by_pr: dict[str, list] = {}
    for cell in cells:
        by_pr.setdefault(cell.group, []).append(cell)
    items = []
    for pr, pr_cells in sorted(by_pr.items()):
        for index, comment in enumerate(references[pr]["comments"]):
            if not comment.get("note"):
                continue
            code, source = code_for_reference(comment, pr_cells)
            lines = f"{comment.get('from_line')}-{comment.get('to_line')} ({comment.get('side') or 'right'} side)"
            prompt = LABEL_PROMPT.format(rubric=RUBRIC, code=code, path=comment.get("path"), lines=lines,
                                         note=comment["note"].strip())
            items.append({"pr": pr, "reference": index, "path": comment.get("path"), "code_from": source,
                          "key": _key(prompt, identity), "prompt": prompt})
    return items


def load_labels(path: Path) -> dict[tuple[str, int], dict]:
    """A label file as {(pr, reference index): record}."""
    path = Path(path)
    if not path.is_file():
        return {}
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return {(r["pr"], int(r["reference"])): r for r in records}


def _write_labels(path: Path, labels: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(labels[k], sort_keys=True, ensure_ascii=False) + "\n" for k in sorted(labels)),
                   encoding="utf-8")
    tmp.replace(path)


def label_references(references: dict[str, dict], cells: list, caller, out_path: Path, concurrency: int = 1,
                     labeller: str = "", progress: Callable[[dict], None] | None = None) -> dict:
    """Label every reference comment of the PRs in `cells` and save the labels to `out_path`
    (one JSON line per reference). Saved labels with the same prompt and model are kept."""
    labels = load_labels(out_path)
    items = reference_items(references, cells, caller.identity)
    todo = [i for i in items
            if labels.get((i["pr"], i["reference"]), {}).get("key") != i["key"]
            or "error" in labels[(i["pr"], i["reference"])]]
    lock = threading.Lock()

    def done(result: dict) -> None:
        result.update(rubric=RUBRIC_VERSION, labeller=labeller)
        with lock:
            labels[(result["pr"], result["reference"])] = result
            _write_labels(out_path, labels)
        if progress:
            progress(result)

    _run(todo, caller, {"tier": TIERS}, concurrency, done)
    wanted = {(i["pr"], i["reference"]) for i in items}
    return {k: v for k, v in labels.items() if k in wanted}


# ---------- the reviewer's findings ----------

def finding_items(reviews: list[dict], judgments: list[dict], cells: list, identity) -> list[dict]:
    """One item per finding that the judge matched to no reference: {entry, level, order, prompt,
    key, ...}. A finding the judge could not judge is left out."""
    text = {(c.entry, c.level): c.text for c in cells}
    judged = {(j["entry"], j["level"]): j for j in judgments}
    items = []
    for review in reviews:
        cell = (review["entry"], review["level"])
        judgment = judged.get(cell)
        if judgment is None or cell not in text:
            continue
        findings = (review.get("parsed") or {}).get("findings", [])
        for record in sorted(judgment["records"], key=lambda r: r["order"]):
            if record.get("matched_gt_indices") or record.get("judge_error") or record["order"] >= len(findings):
                continue
            finding = findings[record["order"]]
            prompt = RATE_PROMPT.format(rubric=RUBRIC, code=text[cell], path=finding.get("path"),
                                        snippet=finding.get("snippet") or "(none)", note=finding["note"].strip())
            items.append({"entry": review["entry"], "level": review["level"], "order": record["order"],
                          "pr": review.get("pr") or review["entry"], "key": _key(prompt, identity), "prompt": prompt})
    return items


def load_ratings(run_dir: Path, folder: str = "ratings") -> dict[tuple[str, str, int], dict]:
    """The saved ratings of a run as {(entry, level, finding order): record}. `folder` names
    the set: one rater's ratings live in one folder of the run, so two raters can be compared."""
    ratings = {}
    for path in sorted((Path(run_dir) / folder).glob("*/*.json")):
        for record in json.loads(path.read_text(encoding="utf-8")):
            ratings[(record["entry"], record["level"], int(record["order"]))] = record
    return ratings


def rate_findings(reviews: list[dict], judgments: list[dict], cells: list, caller, run_dir: Path,
                  concurrency: int = 1, rater: str = "", progress: Callable[[dict], None] | None = None,
                  folder: str = "ratings") -> dict:
    """Rate every unmatched finding of a run and save the ratings under `run_dir`/`folder`, one
    file per (entry, level). Identical prompts are sent once; saved ratings are kept."""
    ratings = load_ratings(run_dir, folder)
    items = finding_items(reviews, judgments, cells, caller.identity)
    by_key = {r["key"]: r for r in ratings.values() if "error" not in r}
    todo, waiting = {}, []
    for item in items:
        saved = ratings.get((item["entry"], item["level"], item["order"]))
        if saved and saved.get("key") == item["key"] and "error" not in saved:
            continue
        if item["key"] not in by_key and item["key"] not in todo:
            todo[item["key"]] = item
        waiting.append(item)
    lock = threading.Lock()

    def done(result: dict) -> None:
        with lock:
            by_key[result["key"]] = result
        if progress:
            progress(result)

    _run(list(todo.values()), caller, {"validity": VALIDITIES, "tier": TIERS}, concurrency, done)
    for item in waiting:
        answer = by_key.get(item["key"], {})
        record = {k: v for k, v in item.items() if k != "prompt"}
        record.update({k: v for k, v in answer.items() if k not in ("entry", "level", "order", "pr")},
                      rubric=RUBRIC_VERSION, rater=rater)
        ratings[(item["entry"], item["level"], item["order"])] = record
    files: dict[tuple[str, str], list] = {}
    for (entry, level, _), record in sorted(ratings.items()):
        files.setdefault((entry, level), []).append(record)
    for (entry, level), records in files.items():
        path = Path(run_dir) / folder / entry / f"{level}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    wanted = {(i["entry"], i["level"], i["order"]) for i in items}
    return {k: v for k, v in ratings.items() if k in wanted}
