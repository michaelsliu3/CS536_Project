"""The reviewer's retry rule, for a reply that did not end in an answer.

A cell is one reviewer call. Two things leave a cell without a readable answer, and each gets one
more call (decided 2026-11-14, docs/outcome_table_first_run.md Section 7):

1. longer_output: the reply was cut off at the output limit. The same request is sent again with
   a larger limit.
2. follow_up: the reply finished without the JSON (prose, or a tool call the reviewer cannot
   make). The conversation continues with one message asking for the JSON. (For a reviewer
   prompt with another reply format the message asks for that format: `follow_up_request`.)

A cell takes each step at most once, in that order, and a follow-up is its last step. The last
reply decides the cell: one that still has no JSON counts as a review with no findings, as
before. The rule is part of the reviewer's definition and applies to every cell. A reply that
finished with its JSON never triggers it, so a cell reviewed before the rule existed keeps its
record and its cache key. A retried record keeps every reply under `retry`:

    "retry": {"policy": <RetryPolicy.identity>,
              "first": {status, reply, truncated},           # the first reply
              "steps": [{step, max_tokens, status, reply, truncated, started_at, finished_at}
                        | {step, skipped: why} | {step, error: what}, ...]}

and its top-level status, reply, parsed and truncated are those of the last reply. A step that
failed (the server did not answer) is tried again by the next run; a skipped one is not.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

from src.review_outcomes.prompts import FOLLOW_UP_REQUEST

RETRY_VERSION = "retry-v1"
LONGER_OUTPUT, FOLLOW_UP = "longer_output", "follow_up"
ANSWERED = ("ok", "malformed", "empty_output")  # statuses of a record that holds a reply
# A step is worth a call only with this much new room for the reply: a longer limit must exceed
# the first one by it, and a follow-up must have it for the JSON.
MIN_REPLY_TOKENS = 1024
# The follow-up's own share of the window: its text, at a generous two characters per token, plus
# the chat template's tokens around the two added turns.
FOLLOW_UP_TOKENS = len(FOLLOW_UP_REQUEST) // 2 + 64  # for the Codex follow-up; see RetryPolicy.follow_up_tokens


@dataclass(frozen=True)
class RetryPolicy:
    longer_max_tokens: int | None = None  # the output limit for a reply that was cut off; None: no such step
    follow_up: bool = False               # ask once for the JSON when a reply finished without it
    context_window: int | None = None     # the model's window in tokens; None: limits are not capped by it
    # The follow-up message. It names the reply format, so it belongs to the reviewer prompt
    # (prompts.ReviewPrompt.follow_up); the default is the Codex prompt's.
    follow_up_request: str = FOLLOW_UP_REQUEST

    @classmethod
    def from_spec(cls, spec: dict, follow_up_request: str | None = None) -> "RetryPolicy | None":
        """The policy in a reviewer profile's "retry" entry, or None when it has none.
        `follow_up_request` is the run's reviewer prompt's follow-up message."""
        retry = spec.get("retry")
        if not retry:
            return None
        return cls(retry.get("longer_max_tokens"), bool(retry.get("follow_up")), retry.get("context_window"),
                   follow_up_request or FOLLOW_UP_REQUEST)

    @property
    def follow_up_tokens(self) -> int:
        """The follow-up's own share of the window (see FOLLOW_UP_TOKENS)."""
        return len(self.follow_up_request) // 2 + 64

    @property
    def identity(self) -> dict:
        """Everything about the rule that changes a retried reply; saved with each retried record."""
        return {"version": RETRY_VERSION, "longer_max_tokens": self.longer_max_tokens, "follow_up": self.follow_up,
                "follow_up_sha256": (hashlib.sha256(self.follow_up_request.encode("utf-8")).hexdigest()
                                     if self.follow_up else None),
                "context_window": self.context_window}


def next_step(record: dict, policy: RetryPolicy | None) -> str | None:
    """The step this record is due under the policy, or None."""
    if policy is None or record.get("status") not in ANSWERED:
        return None
    taken = {step["step"] for step in (record.get("retry") or {}).get("steps", []) if "error" not in step}
    if FOLLOW_UP in taken:
        return None
    if record.get("truncated"):
        return LONGER_OUTPUT if policy.longer_max_tokens and LONGER_OUTPUT not in taken else None
    if record["status"] != "ok":
        return FOLLOW_UP if policy.follow_up else None
    return None


def step_limit(step: str, record: dict, policy: RetryPolicy, first_limit: int | None) -> tuple[int | None, str | None]:
    """(output limit to send the step with, None), or (None, why the step cannot run).

    `first_limit` is the reviewer's normal output limit. The model's window holds the prompt and
    the reply together, so a cell near the input cap may have no room for the step.
    """
    reply = record["reply"]
    prompt_tokens, window = reply.get("prompt_tokens"), policy.context_window
    if step == LONGER_OUTPUT:
        limit = policy.longer_max_tokens
        if window is not None and prompt_tokens is not None:
            limit = min(limit, window - prompt_tokens)
        if limit - (first_limit or 0) < MIN_REPLY_TOKENS:
            return None, (f"no room for a longer reply: the prompt has {prompt_tokens} tokens and the window {window}, "
                          f"so a reply could have {limit} tokens, under {MIN_REPLY_TOKENS} more than the first limit "
                          f"of {first_limit}")
        return limit, None
    limit = first_limit
    if window is not None and prompt_tokens is not None:
        room = window - prompt_tokens - (reply.get("completion_tokens") or 0) - policy.follow_up_tokens
        limit = room if limit is None else min(limit, room)
        if limit < MIN_REPLY_TOKENS:
            return None, (f"no room for a follow-up: the prompt and the first reply leave about {max(room, 0)} tokens "
                          f"of the {window}-token window, fewer than {MIN_REPLY_TOKENS}")
    return limit, None


def _answer(record: dict) -> dict:
    return {"status": record.get("status"), "reply": record.get("reply"), "truncated": record.get("truncated")}


def align(record: dict, policy: RetryPolicy | None, parse: Callable[[str], dict]) -> dict:
    """The record as this policy must see it. Retries made under another policy (or with none
    set now) are undone, which puts the first reply back in force; the caller then retries it
    under the current policy. A record without retries, or retried under this policy, is returned
    as it is."""
    saved = record.get("retry")
    if not saved or (policy is not None and saved.get("policy") == policy.identity):
        return record
    first = saved["first"]
    parsed = parse(first["reply"]["text"])
    restored = {key: value for key, value in record.items() if key != "retry"}
    restored.update(status=parsed["status"], reply=first["reply"], parsed=parsed, truncated=bool(first.get("truncated")))
    return restored


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_steps(record: dict, messages: list[dict], reviewer: Callable, policy: RetryPolicy,
              parse: Callable[[str], dict], report: Callable[[dict, dict], None] = lambda entry, record: None) -> dict:
    """Apply every step the record is due, in order, and return the updated copy.

    `messages` is the cell's original request, `reviewer(messages, max_tokens=...)` the backend,
    `parse(text)` the reply parser. `report(entry, record)` is called after each step with the
    step's entry and the record as it then stands.
    """
    record = dict(record)
    if "retry" in record:
        record["retry"] = {**record["retry"], "steps": list(record["retry"]["steps"])}
    first_limit = getattr(reviewer, "max_tokens", None)
    while (step := next_step(record, policy)) is not None:
        limit, why = step_limit(step, record, policy, first_limit)
        entry: dict = {"step": step}
        if why:
            entry["skipped"] = why
        else:
            entry.update(max_tokens=limit, started_at=_now())
            request = messages if step == LONGER_OUTPUT else messages + [
                {"role": "assistant", "content": record["reply"]["text"]},
                {"role": "user", "content": policy.follow_up_request}]
            try:
                entry["reply"] = reviewer(request) if limit is None else reviewer(request, max_tokens=limit)
            except Exception as error:  # noqa: BLE001 - recorded; the next run tries this step again
                entry["error"] = f"{type(error).__name__}: {error}"[:2000]
            entry["finished_at"] = _now()
        retry = record.setdefault("retry", {"policy": policy.identity, "first": _answer(record), "steps": []})
        retry["steps"] = [s for s in retry["steps"] if not (s["step"] == step and "error" in s)] + [entry]
        if "reply" in entry:
            parsed = parse(entry["reply"]["text"])
            entry.update(status=parsed["status"], truncated=entry["reply"].get("finish_reason") == "length")
            record.update(status=parsed["status"], reply=entry["reply"], parsed=parsed, truncated=entry["truncated"])
        report(entry, record)
        if "error" in entry:
            break
    return record


def steps_taken(record: dict) -> str | None:
    """The steps that produced a reply for this record, e.g. "longer_output+follow_up"; None if none did."""
    names = [step["step"] for step in (record.get("retry") or {}).get("steps", []) if "reply" in step]
    return "+".join(names) or None


def answer_id(record: dict) -> str | None:
    """Identifies a retried record's answer in force, for anything cached on top of the review:
    the record's cache key covers the first call only. None for a record without retries."""
    if not record.get("retry"):
        return None
    text = (record.get("reply") or {}).get("text") or ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
