"""Severity labels for reference comments (docs/review_score_design.md).

`label_references` gives each reference comment a tier (blocking, should_fix, nit). The model
sees the comment and the code it is about, not who wrote the comment. Labels are made once and
saved; a later run skips what is saved under the same prompt, model settings and input. The
model is the caller's choice (a chat backend from backends.py).
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
