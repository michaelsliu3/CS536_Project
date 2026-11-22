"""Labels for reference comments that come without any: human review comments taken from pull
requests (the ContextCRBench pilot), where AACR-Bench's comments arrive already labelled.

One call per comment gives:
  valid     whether the comment is a finding about the code shown, or something else (a question
            with no problem behind it, praise, process talk, a claim its own thread withdraws);
  finding   the concern in one plain sentence, kept beside the comment and never in place of it;
  category  AACR-Bench's four comment categories;
  context   AACR-Bench's three context levels: what must be seen to raise the comment;
  tier      the severity rubric of severity.py.

The model sees the code the comment is about, the comment and the replies in its thread. The
replies are shown because they can settle whether the reviewer was right. Labels are saved once
per comment, under the same key rule as severity.py, and a later run keeps what is unchanged.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Callable

from src.review_outcomes import severity

LABEL_VERSION = "reference-labels-v1"
CATEGORIES = ("Code Defect", "Maintainability and Readability", "Performance", "Security Vulnerability")
CONTEXTS = ("Diff Level", "File Level", "Repo Level")
NONE = "none"  # the answer for category, context and tier of a comment that is not valid

LABEL_PROMPT = """\
A human reviewer left the comment below on a pull request. Decide whether it can serve as a \
reference finding for judging an automated code reviewer, and describe it.

The code below is what the reviewer saw. Judge from the code and from the replies, not from how \
sure the comment sounds.

## Code under review

{code}

## Review comment

File: {path}
Lines: {lines}

{note}

## Replies in the same thread

{replies}

## What to answer

valid: "yes" when the comment names a concrete problem with the code shown, or a concrete \
improvement to it, that a reviewer of this code could raise. "no" when it is a question with no \
problem behind it, praise or thanks, talk about process (rebasing, CI, who should review), a note \
the reviewer wrote for themselves, a point the replies show to be mistaken or withdrawn, or \
something that cannot be understood from what is shown.

finding: the problem or improvement in one plain sentence, as a reviewer would report it. Use "" \
when valid is "no".

category: one of {categories}.

context: what a reviewer must see to raise the comment. "Diff Level": the changed lines are \
enough. "File Level": other code of the same file is needed. "Repo Level": code or knowledge \
outside this file is needed.

tier: how much the comment matters if it is right.
{rubric}

When valid is "no", answer "{none}" for category, context and tier.

## Answer

Reply with one JSON object and nothing else:
{{"valid": "yes" | "no", "kind": "<two to five words naming the kind of comment>", "finding": "<one sentence or empty>", \
"category": "<category>", "context": "<context>", "tier": "blocking" | "should_fix" | "nit" | "{none}", \
"reason": "<one sentence>", "confidence": <0 to 1>}}"""

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_CHOICES = {"valid": {"yes": True, "no": False, "true": True, "false": False},
            "category": {c.lower(): c for c in CATEGORIES} | {NONE: None},
            "context": {c.lower(): c for c in CONTEXTS} | {NONE: None},
            "tier": {t: t for t in severity.TIERS} | {"should fix": "should_fix", "should-fix": "should_fix", NONE: None}}


def parse_labels(text: str) -> dict | None:
    """The labels in a reply: its last JSON object that answers every question with an allowed
    value, or None. A valid comment must have a category, a context and a tier. Thinking and
    text around the object are ignored; braces inside strings are fine."""
    body = _THINK.sub("", text or "")
    if "</think>" in body:
        body = body.split("</think>", 1)[1]
    decoder = json.JSONDecoder()
    for start in reversed([m.start() for m in re.finditer(r"\{", body)]):
        try:
            answer, _ = decoder.raw_decode(body[start:])
        except json.JSONDecodeError:
            continue
        if not isinstance(answer, dict):
            continue
        labels = {}
        for field, allowed in _CHOICES.items():
            value = str(answer.get(field, "")).strip().lower()
            if value not in allowed:
                break
            labels[field] = allowed[value]
        else:
            if labels["valid"] and None in (labels["category"], labels["context"], labels["tier"]):
                continue
            labels["finding"] = str(answer.get("finding") or "").strip()
            labels["kind"] = str(answer.get("kind") or "").strip()
            for extra in ("reason", "confidence"):
                if extra in answer:
                    labels[extra] = answer[extra]
            return labels
    return None


def reference_items(references: dict[str, dict], cells: list, identity) -> list[dict]:
    """One item per reference comment of the PRs in `cells`: {pr, reference, thread, prompt, key, ...}."""
    by_pr: dict[str, list] = {}
    for cell in cells:
        by_pr.setdefault(cell.group, []).append(cell)
    items = []
    for pr, pr_cells in sorted(by_pr.items()):
        for index, comment in enumerate(references[pr]["comments"]):
            if not comment.get("note"):
                continue
            code, source = severity.code_for_reference(comment, pr_cells)
            replies = "\n\n".join(f"Reply {n}: {reply.strip()}" for n, reply in enumerate(comment.get("replies") or [], 1))
            prompt = LABEL_PROMPT.format(
                code=code, path=comment.get("path"), note=comment["note"].strip(), replies=replies or "(no replies)",
                lines=f"{comment.get('from_line')}-{comment.get('to_line')} ({comment.get('side') or 'right'} side)",
                categories=", ".join(f'"{c}"' for c in CATEGORIES), rubric=severity.RUBRIC, none=NONE)
            key = severity._sha(json.dumps({"prompt": severity._sha(prompt), "backend": identity, "version": LABEL_VERSION},
                                           sort_keys=True))
            items.append({"pr": pr, "reference": index, "thread": comment.get("thread"), "path": comment.get("path"),
                          "code_from": source, "key": key, "prompt": prompt})
    return items


def label_references(references: dict[str, dict], cells: list, caller, out_path: Path, concurrency: int = 1,
                     labeller: str = "", progress: Callable[[dict], None] | None = None) -> dict:
    """Label every reference comment of the PRs in `cells` and save the labels to `out_path`, one
    JSON line per comment. Saved labels with the same prompt and model are kept; a reply that
    cannot be read is saved as an error and asked again by the next run."""
    from concurrent.futures import ThreadPoolExecutor

    labels = severity.load_labels(out_path)
    items = reference_items(references, cells, caller.identity)
    todo = [i for i in items
            if labels.get((i["pr"], i["reference"]), {}).get("key") != i["key"] or "error" in labels[(i["pr"], i["reference"])]]
    lock = threading.Lock()

    def one(item: dict) -> dict:
        result = {k: v for k, v in item.items() if k != "prompt"}
        result.update(version=LABEL_VERSION, labeller=labeller)
        try:
            reply = caller([{"role": "user", "content": item["prompt"]}])
            answer = parse_labels(reply["text"])
            if answer is None:
                result.update(error="unreadable reply", reply=reply["text"][-2000:])
            else:
                result.update(answer, output_tokens=reply.get("completion_tokens"), seconds=reply.get("seconds"))
        except Exception as error:  # noqa: BLE001 - one failed call must not stop the run
            result["error"] = f"{type(error).__name__}: {error}"
        return result

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for result in pool.map(one, todo):
            with lock:
                labels[(result["pr"], result["reference"])] = result
                severity._write_labels(out_path, labels)
            if progress:
                progress(result)
    wanted = {(i["pr"], i["reference"]) for i in items}
    return {k: v for k, v in labels.items() if k in wanted}
