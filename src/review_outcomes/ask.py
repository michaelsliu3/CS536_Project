"""Asking the reviewer, after its review of a hunk, whether it needs to see more code.

This is the request half of a feedback loop: the reviewer reviews a hunk at a cheap level, is
then asked whether the function and the file around the change would be likely to change that
review, and a selector or a fixed rule decides whether to grant the request with a second
review at a wider level.

The question follows the saved review in the same conversation. The review itself is therefore
unchanged: the same prompt, the same saved reply. The answer is saved next to the run, one line
per hunk, and a later run keeps the answers whose conversation and model settings are unchanged.
No reference comment is shown, so the answer can be used as a selector's input.
"""
from __future__ import annotations

import hashlib
import json
import re
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

ASK_VERSION = "ask-file-v1"

_QUESTION = """The review above was written from the pull request text and this one diff hunk. Before it is final, you can be shown more code: the whole function around the change and the rest of the file the hunk is in.

Would seeing that code be likely to change your review, by revealing a problem you could not see, or by confirming or withdrawing a comment you made?
{cost}
Answer with one JSON object and nothing else:
{{"need_file": "yes" or "no", "likelihood": 1 to 5, "missing": "..."}}

- "likelihood": 1 means the review would almost surely stay the same, 5 means it would almost surely change.
- "missing": what you would need to see, in a few words; "" if nothing."""

_COST = """
The extra code is not free: it is usually several times longer than the hunk, and most hunks can be reviewed without it. Answer yes only when something specific that the review depends on is not visible in the hunk.
"""

# "plain" asks the question; "cost" also tells the reviewer that a request has a price.
VARIANTS = {"plain": _QUESTION.format(cost=""), "cost": _QUESTION.format(cost=_COST)}

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_OBJECT = re.compile(r"\{[^{}]*\}", re.DOTALL)
_YES_NO = {"yes": True, "true": True, "no": False, "false": False}


def ask_messages(review_messages: list[dict], review_text: str, variant: str) -> list[dict]:
    """The review's own messages, its saved reply, and the question."""
    return [*review_messages, {"role": "assistant", "content": review_text},
            {"role": "user", "content": VARIANTS[variant]}]


def parse_ask(text: str) -> dict | None:
    """{need_file, likelihood, missing} from the last JSON object in the reply that answers yes
    or no, or None. `likelihood` is None when it is not a whole number from 1 to 5."""
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
        need = _YES_NO.get(str(answer.get("need_file", "")).strip().lower())
        if need is None:
            continue
        likelihood = answer.get("likelihood")
        if isinstance(likelihood, str) and likelihood.strip().isdigit():
            likelihood = int(likelihood)
        if isinstance(likelihood, bool) or not isinstance(likelihood, int) or not 1 <= likelihood <= 5:
            likelihood = None
        return {"need_file": need, "likelihood": likelihood, "missing": str(answer.get("missing") or "").strip()}
    return None


def _key(messages: list[dict], identity) -> str:
    text = json.dumps({"messages": messages, "backend": identity, "version": ASK_VERSION}, sort_keys=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def asks_path(run_dir: Path, level: str, variant: str) -> Path:
    return Path(run_dir) / "asks" / f"{level}_{variant}.jsonl"


def load_asks(path: Path) -> dict[str, dict]:
    """{entry: record} from an answers file; {} when there is none."""
    path = Path(path)
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as handle:
        return {record["entry"]: record for record in map(json.loads, filter(str.strip, handle))}


def _write(path: Path, asks: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for entry in sorted(asks):
            handle.write(json.dumps(asks[entry], ensure_ascii=False, sort_keys=True) + "\n")
    tmp.replace(path)


def ask_items(cells: list, reviews: dict[str, dict], prompt, identity, variant: str) -> list[dict]:
    """One item per cell that has a finished review: {entry, pr, level, messages, key}. `reviews`
    is {entry: the saved review record of that cell}."""
    items = []
    for cell in cells:
        review = reviews.get(cell.entry)
        if not review or review.get("status") != "ok" or not (review.get("reply") or {}).get("text"):
            continue
        messages = ask_messages(prompt.messages(cell.text, cell.parts), review["reply"]["text"], variant)
        items.append({"entry": cell.entry, "pr": cell.pr, "level": cell.level, "variant": variant,
                      "version": ASK_VERSION, "review_key": review.get("key"), "messages": messages,
                      "key": _key(messages, identity)})
    return items


def run_asks(items: list[dict], caller: Callable[[list[dict]], dict], out_path: Path, concurrency: int = 1,
             progress: Callable[[dict], None] | None = None) -> dict[str, dict]:
    """Ask each item's question unless its answer is saved, and return {entry: record} for the
    items. A reply that cannot be read is saved as an error and asked again by the next run."""
    saved = load_asks(out_path)
    asks = {i["entry"]: saved[i["entry"]] for i in items
            if saved.get(i["entry"], {}).get("key") == i["key"] and "error" not in saved[i["entry"]]}
    todo = [i for i in items if i["entry"] not in asks]

    def one(item: dict) -> dict:
        result = {k: v for k, v in item.items() if k != "messages"}
        try:
            reply = caller(item["messages"])
            answer = parse_ask(reply["text"])
            if answer is None:
                result.update(error="unreadable reply", reply=reply["text"][-2000:])
            else:
                result.update(answer, prompt_tokens=reply.get("prompt_tokens"),
                              output_tokens=reply.get("completion_tokens"), seconds=reply.get("seconds"))
        except Exception as error:  # noqa: BLE001 - one failed call must not stop the run
            result["error"] = f"{type(error).__name__}: {error}"
        return result

    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        for count, result in enumerate(pool.map(one, todo), 1):
            asks[result["entry"]] = result
            if progress:
                progress(result)
            if count % 25 == 0:
                _write(out_path, {**saved, **asks})
    if todo:
        _write(out_path, {**saved, **asks})
    return asks
