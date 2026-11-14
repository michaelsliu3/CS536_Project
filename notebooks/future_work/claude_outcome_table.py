"""Build the (PR x level) outcome table with Claude as the frozen reviewer, through the Claude API
or through Claude Code on a Claude plan's sign-in.

Future work, kept apart from the main pipeline. notebooks/outcome_table.ipynb reviews with
Qwen/Qwen3.8-27B on a cloud GPU, and the roadmap (Section 8.5.2) allows Claude only as a
fallback. This script writes a separate table in its own run folder, with the same prompt,
parser, AACR judge metric and file layout (src/review_outcomes/), so the two can be compared.
It needs no GPU: a laptop with data/aacr/ is enough.

One review is one Messages API call: the Codex /review rubric as the system prompt, and the
request sentence plus the level's context as the user message. Defaults: claude-opus-5-5 at
effort medium, sent through the Message Batches API (half price, results within a day).

    P=notebooks/future_work/claude_outcome_table.py
    .venv/bin/python $P plan                      # what would be sent and a cost estimate; no network
    .venv/bin/python $P count                     # exact input tokens from the API (free)
    .venv/bin/python $P review --first 15         # review a 15-PR pilot (asks before spending)
    .venv/bin/python $P judge --judge claude      # score the reviews, then write the table
    .venv/bin/python $P table                     # rebuild table.csv from the saved records
    .venv/bin/python $P status                    # batches and saved replies

Without the API, on a Claude plan's sign-in (its usage limits apply, and it has its own run folder):

    .venv/bin/python $P code-login                # sign Claude Code in once
    .venv/bin/python $P code-check                # two tiny calls: is it signed in, what does it add
    .venv/bin/python $P review --via claude-code --first 15
    .venv/bin/python $P judge --via claude-code --first 15 --judge claude

Every step can be stopped and run again at any point, and a request that was paid for is not
sent a second time: each reply is saved once under <run folder>/api/replies/ and reused, a
submitted batch is recorded before anything else happens, and only one process works in a run
folder at a time. See "Stopping and resuming" in README.md in this folder.
"""
from __future__ import annotations

import argparse
import hashlib
import http.server
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.review_outcomes import backends, table  # noqa: E402
from src.review_outcomes.judge import score_review  # noqa: E402
from src.review_outcomes.parse import PARSER_VERSION, extract_review_object  # noqa: E402
from src.review_outcomes.prompts import (  # noqa: E402
    JUDGE_PROMPT_VERSION, REVIEW_PROMPT_VERSION, REVIEW_REQUEST, RUBRIC_SHA256, load_rubric, prompt_sha256,
    review_messages,
)

DEFAULT_MODEL = "claude-opus-5-5"
DEFAULT_EFFORT = "medium"
EFFORTS = ("low", "medium", "high", "xhigh", "max")
DEFAULT_MAX_TOKENS = 32_000  # thinking counts toward it; only the tokens actually written are billed
DEFAULT_JUDGE_MAX_TOKENS = 8_000
# Standard USD prices per million tokens (input, output), from Anthropic's pricing page on
# 2026-11-13. Message Batches cost half. Use --price-in/--price-out for other models or new prices.
PRICES = {"claude-opus-5-5": (4.0, 20.0), "claude-sonnet-5-5": (2.0, 10.0)}
PRICES_AS_OF = "2026-11-13"
BATCH_DISCOUNT = 0.5
BATCH_MAX_REQUESTS = 100_000  # the API's limits for one batch: 100,000 requests or 256 MB
BATCH_MAX_MB = 256
UPLOAD_TIMEOUT = 1800  # seconds one batch upload may take
CLOCK_SKEW = 120  # seconds this machine's clock may differ from the API's
UPLOAD_GRACE = 120  # seconds to wait for a batch from an interrupted upload to show up before sending again
PROMPT_TOKENS = 1_710  # rubric, request sentence and chat template; the dataset's counts leave it out
SCORABLE = ("ok", "malformed", "empty_output")
STOP_TO_FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}
REQUEST_ID = re.compile(r"^[0-9a-f]{64}$")
VIA_API, VIA_CODE = "api", "claude-code"  # how a request reaches the model: the Claude API, or Claude Code's print mode
DESKTOP_BUILDS = Path.home() / "Library" / "Application Support" / "Claude" / "claude-code"  # the macOS desktop app's
CODE_TIMEOUT = 3600  # seconds one Claude Code call may take
SCRIPT = "notebooks/future_work/claude_outcome_table.py"
SIGN_IN_HELP = (f"Claude Code is not signed in on this machine (the desktop app's own sign-in is not shared with "
                f"scripts). Sign in once with your Claude plan:\n    .venv/bin/python {SCRIPT} code-login\n"
                f"then check it:\n    .venv/bin/python {SCRIPT} code-check")


class OutOfCredits(RuntimeError):
    """The API organization has no credit left; nothing more can be sent until credits arrive."""


class LimitReached(RuntimeError):
    """The Claude plan's usage limit (or the cap this run was given) is reached; nothing more can
    be sent until it resets. `reset` is when that is, in seconds since the epoch, if known."""

    def __init__(self, message: str, reset: float | None = None):
        super().__init__(message)
        self.reset = reset


class ExtraUsage(RuntimeError):
    """A request was served from paid extra usage. The run must stop at once."""


class TooManyFailures(RuntimeError):
    """Several requests in a row failed for another reason; continuing would only repeat that."""


class StopTime(RuntimeError):
    """The time given with --until has come; nothing more is sent."""


def parse_until(text: str | None) -> float | None:
    """--until as seconds since the epoch: "YYYY-MM-DD HH:MM", or "HH:MM" for the next time the
    clock shows it. Local time."""
    if not text:
        return None
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M").timestamp()
    except ValueError:
        pass
    try:
        clock = datetime.strptime(text, "%H:%M")
    except ValueError:
        raise SystemExit(f'--until takes "YYYY-MM-DD HH:MM" or "HH:MM", not {text!r}') from None
    moment = datetime.now().replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
    return moment.timestamp() + (0 if moment > datetime.now() else 86400)


class Rejected(ValueError):
    """The request itself cannot be answered, for example because it is longer than the context window."""


def out_of_credits(message: str) -> bool:
    return bool(re.search(r"credit balance|billing_error|purchase credits", message or "", re.IGNORECASE))


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def user_message(prompt: str) -> list[dict]:
    return [{"role": "user", "content": prompt}]  # how backends.judge_caller sends a judge prompt


# --------------------------------------------------------------------------- requests and replies

@dataclass(frozen=True)
class ClaudeSpec:
    """Everything that defines a call besides its prompt."""
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT
    max_tokens: int = DEFAULT_MAX_TOKENS
    replicate: int = 0  # another number draws an independent sample of every reply (to measure noise)
    channel: str = VIA_API  # VIA_API, or VIA_CODE for Claude Code's print mode on the plan's sign-in

    @property
    def identity(self) -> dict:
        """Goes into the outcome table's cache keys, like the other backends' `identity`."""
        return {"kind": "anthropic-messages" if self.channel == VIA_API else "claude-code-print",
                "model": self.model, "effort": self.effort,
                "thinking": "adaptive (the model's default); no sampling parameters",
                "max_tokens": self.max_tokens, "replicate": self.replicate}

    def params(self, messages: list[dict]) -> dict:
        """The Messages API request for these chat messages.

        It sends no `thinking` (current models think adaptively by default, and Claude Opus 5.5
        accepts nothing else), no `temperature` (rejected by current models), and no `fallbacks`:
        another model's reply must never enter this reviewer's table."""
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        params = {"model": self.model, "max_tokens": self.max_tokens, "output_config": {"effort": self.effort},
                  "messages": [m for m in messages if m["role"] != "system"]}
        if system:
            params["system"] = system
        return params

    def request_id(self, messages: list[dict]) -> str:
        """sha256 of the request and the replicate number: the reply store's key and the batch
        `custom_id`. A stored reply with this id is by construction the answer to this request.

        `max_tokens` is left out. It changes a reply only by cutting it off, and a cut-off reply
        is asked for again only once the limit is raised (ReplyStore.answered), so raising the
        limit never repeats the reviews that were complete."""
        params = self.params(messages)
        del params["max_tokens"]
        payload = {"params": params, "replicate": self.replicate}
        if self.channel != VIA_API:
            payload["channel"] = self.channel  # the two channels are different reviewers and never share replies
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


class ReplyStore:
    """Saved API replies, one JSON file per request id. Claude does not decode greedily, so a
    reply is saved once and reused by every later step and run; a cell is never re-sampled
    silently. Requests the API rejected as invalid are remembered too, and not sent again."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, kind: str, rid: str) -> Path:
        return self.root / kind / rid[:2] / f"{rid}.json"

    def get(self, rid: str) -> dict | None:
        return table.read_json(self._path("replies", rid))

    @staticmethod
    def cut_short(reply: dict, max_tokens: int) -> bool:
        """The reply stopped at its output limit, and that limit was below `max_tokens`."""
        return reply.get("finish_reason") == "length" and (reply.get("max_tokens") or 0) < max_tokens

    def answered(self, rid: str, max_tokens: int) -> bool:
        """Whether a saved reply settles this request, so that it must not be sent again. A
        complete reply always does. A cut-off reply does too, until the limit is raised above
        the one it was made with."""
        reply = self.get(rid)
        return reply is not None and not self.cut_short(reply, max_tokens)

    def put(self, rid: str, reply: dict) -> bool:
        """Save a reply unless one is already saved: the first reply to a request is the one every
        later step uses, whatever arrives afterwards. The one exception is a cut-off reply, which
        a reply made with a higher output limit replaces. Returns whether it was saved."""
        saved = self.get(rid)
        if saved is not None and not self.cut_short(saved, reply.get("max_tokens") or 0):
            return False
        table.write_json(self._path("replies", rid), reply)
        self._path("rejected", rid).unlink(missing_ok=True)
        return True

    def rejection(self, rid: str) -> dict | None:
        return table.read_json(self._path("rejected", rid))

    def reject(self, rid: str, kind: str, message: str) -> None:
        table.write_json(self._path("rejected", rid), {"type": kind, "message": message[:2000], "at": now()})

    def token_count(self, rid: str) -> int | None:
        saved = table.read_json(self._path("token_counts", rid))
        return saved["input_tokens"] if saved else None

    def put_token_count(self, rid: str, tokens: int) -> None:
        table.write_json(self._path("token_counts", rid), {"input_tokens": tokens, "at": now()})


class PendingReply(LookupError):
    """The request has not been answered yet."""


class ClaudeChat:
    """The reviewer or judge as src/review_outcomes calls it: chat messages in, reply out. It only
    reads the store; `fulfil` is what calls the API. An unanswered request raises, which the
    outcome-table code records as an error to retry on the next run."""

    def __init__(self, spec: ClaudeSpec, store: ReplyStore, known_paths: dict[str, tuple] | None = None):
        """`known_paths` (request id -> the files of that PR) is given for the reviewer only: it
        turns on reading a review that was written as Markdown (see `markdown_review`). The
        identity says so, so review records made without it are rebuilt, at no cost."""
        self.spec, self.store, self.known_paths = spec, store, known_paths
        self.identity = spec.identity if known_paths is None else {**spec.identity, "markdown_replies": MARKDOWN_VERSION}

    def __call__(self, messages: list[dict]) -> dict:
        rid = self.spec.request_id(messages)
        reply = self.store.get(rid)
        if reply is not None:
            return reply if self.known_paths is None else as_json_review(reply, self.known_paths.get(rid, ()))
        rejected = self.store.rejection(rid)
        if rejected:
            raise RuntimeError(f"the API rejected this request ({rejected['type']}): {rejected['message']}")
        raise PendingReply("no saved Claude reply for this request yet; run the review or judge step")


# --------------------------------------------------------------------------- reviews written as Markdown
#
# About one reply in six is a complete review written as a Markdown list instead of the JSON
# object the rubric asks for: a "**Findings**" heading, one "**[P2] title**" item per finding
# with its file, lines and confidence, then "**Overall verdict: patch is incorrect**". The shared
# parser reads JSON only and would score such a reply as an empty review. `markdown_review` reads
# the list into the rubric's JSON shape. It asks no model and guesses nothing: a reply it does not
# recognise stays "malformed", and the saved reply itself is never changed.

MARKDOWN_VERSION = "markdown-findings-v1"
_MD_MARKER = r" ?(?:(?:[-*+]|\d+[.)])\s+|#{2,6}\s+(?:\d+[.)]\s+)?)?"  # a list or heading marker, not indented
_MD_ITEM = re.compile(rf"^{_MD_MARKER}(?:\*\*)?\[P([0-3])\]")  # a finding starts with its priority tag
_MD_OVERALL = re.compile(r"^(?:#{1,6}\s*|\*\*)Overall\b|^Overall(?:\s+\w+)?\s*:", re.IGNORECASE)
_MD_VERDICT = re.compile(r"patch\s+is\s+(in)?correct", re.IGNORECASE)
_MD_CONFIDENCE = re.compile(r"(?:overall\s+)?confidence(?:\s+score)?[\s:*=]*([01](?:\.\d+)?)", re.IGNORECASE)
_MD_CODE = re.compile(r"`([^`\n]+)`")
_MD_LINES = re.compile(r"\blines?\s+(\d+)(?:\s*(?:[-–—]|to)\s*(\d+))?", re.IGNORECASE)
_MD_LABEL = re.compile(r"^\s*(?:[-*+]\s+)?(?:\*\*)?(location|file|path|confidence)\s*(?:\*\*)?\s*:", re.IGNORECASE)


def _md_location(text: str):
    """(path, first line, last line, (from, to) within `text`) of the first file named in backticks."""
    for match in _MD_CODE.finditer(text):
        token = match.group(1).strip()
        suffix = re.search(r":(\d+)(?:-(\d+))?$", token)  # `src/a.py:10-20`
        path = token[:suffix.start()] if suffix else token
        if not re.fullmatch(r"[\w.@+~/\\\[\]-]+", path) or not ("/" in path or re.search(r"\.[A-Za-z]\w{0,7}$", path)):
            continue  # code such as `end()` or `std::getline`, not a file
        lines = None if suffix else _MD_LINES.search(text, match.end())
        if lines and (lines.start() - match.end() > 40 or "\n" in text[match.end():lines.start()]):
            lines = None  # line numbers that belong to something else
        first, last = (suffix or lines).groups() if (suffix or lines) else (None, None)
        return (path, int(first) if first else None, int(last or first) if first else None,
                (match.start(), lines.end() if lines else match.end()))
    return None


def _md_confidence(text: str):
    """(value, (from, to) within `text`) of a stated confidence between 0 and 1."""
    match = _MD_CONFIDENCE.search(text)
    if match and float(match.group(1)) <= 1:
        return float(match.group(1)), match.span()
    return None


def _md_resolve(path: str, known: set[str]) -> str:
    """The PR's file that `path` names: itself, or the one known file it is the end of (a review
    may write `Tools.cpp` for `src/App/Tools.cpp`). AACR's judge compares paths exactly."""
    clean = path.replace("\\", "/")
    while clean.startswith("./"):
        clean = clean[2:]
    if clean in known:
        return clean
    ends = [name for name in known if name.endswith("/" + clean)]
    return ends[0] if len(ends) == 1 else clean


def _md_finding(block: list[str], known: set[str]) -> dict | None:
    """One finding from its lines: the title line, then its body."""
    priority = int(_MD_ITEM.match(block[0]).group(1))
    head = re.sub(rf"^{_MD_MARKER}", "", block[0])
    bold = re.match(r"\*\*(.+?)\*\*", head)
    if bold:
        title, rest = bold.group(1), head[bold.end():]
    else:  # a plain title; what follows it on the line starts at the file name, if there is one
        named = _md_location(head)
        cut = named[3][0] if named else len(head)
        title, rest = head[:cut].rstrip(" (—–-:,"), head[cut:]
    location, confidence = _md_location(rest), _md_confidence(rest)
    # Text after the title that is neither the location nor the confidence belongs to the body.
    spans = sorted(found[-1] for found in (location, confidence) if found)
    extra, at = "", 0
    for start, end in spans:
        extra, at = extra + rest[at:start], max(at, end)
    extra = re.sub(r"\b(?:file|location|path)\b\s*:?", "", extra + rest[at:], flags=re.IGNORECASE).strip(" \t()[];,.:—–-*")
    body = []
    for line in block[1:]:
        label = _MD_LABEL.match(line)
        if label and label.group(1).lower() == "confidence":
            if confidence is None and _md_confidence(line):
                confidence = _md_confidence(line)
                continue
        elif label and location is None and _md_location(line):
            location = _md_location(line)
            continue
        body.append(line)
    text = textwrap.dedent("\n".join(body)).strip()
    if re.search(r"[A-Za-z]{3}", extra):
        text = f"{extra}\n\n{text}".strip()
    title = title.strip()
    if not title:
        return None
    finding: dict = {"title": title, "body": text, "priority": priority}
    if confidence:
        finding["confidence_score"] = confidence[0]
    if location:
        path, first, last, _ = location
        finding["code_location"] = {"absolute_file_path": _md_resolve(path, known)}
        if finding["code_location"]["absolute_file_path"] != path:
            finding["code_location"]["path_as_written"] = path
        if first is not None:
            finding["code_location"]["line_range"] = {"start": first, "end": last}
    return finding


def markdown_review(text: str, known_paths=()) -> dict | None:
    """A review written as a Markdown list of findings, in the rubric's JSON shape, or None when
    the text is not such a list. `known_paths` are the PR's files, to complete a bare file name."""
    lines = (text or "").replace("\r\n", "\n").split("\n")
    marks = [(i, "item") for i, line in enumerate(lines) if _MD_ITEM.match(line)]
    marks += [(i, "overall") for i, line in enumerate(lines) if _MD_OVERALL.match(line)][:1]
    marks.sort()
    blocks = [(kind, lines[start:end]) for (start, kind), (end, _) in zip(marks, marks[1:] + [(len(lines), "")])]
    overall = next(("\n".join(block) for kind, block in blocks if kind == "overall"), "")
    verdict = _MD_VERDICT.search(overall)
    items = [block for kind, block in blocks if kind == "item"]
    if not items:
        # No findings: accepted only when the reply says so in a few words, names no file and gives
        # its verdict. Findings in a layout this function does not know must not be read as none.
        before = " ".join(line.strip() for line in (lines[:marks[0][0]] if marks else lines)
                          if not re.fullmatch(r"\W*findings\W*", line, re.IGNORECASE)).strip()
        says_none = re.search(r"\bnone\b|\bno\b.{0,40}\b(?:issues?|findings?|problems?|bugs?)\b|\bnothing to (?:flag|report)\b",
                              before, re.IGNORECASE)
        if not (verdict and says_none and len(before) <= 200 and _md_location(before) is None):
            return None
    known = set(known_paths)
    review: dict = {"findings": []}
    for block in items:
        finding = _md_finding(block, known)
        if finding is None:
            return None
        review["findings"].append(finding)
    if verdict:
        review["overall_correctness"] = "patch is incorrect" if verdict.group(1) else "patch is correct"
        rest = _MD_CONFIDENCE.sub(" ", overall[verdict.end():])
        rest = re.sub(r"\(\s*\)", " ", rest)  # the brackets a removed confidence stood in
        review["overall_explanation"] = re.sub(r"^[\s*.:;,()—–-]+", "", rest).strip()
    confidence = _md_confidence(overall)
    if confidence:
        review["overall_confidence_score"] = confidence[0]
    return review


def as_json_review(reply: dict, known_paths=()) -> dict:
    """The reply as the parser can read it: unchanged when it holds the rubric's JSON object (or is
    not a review this script can read), otherwise with the Markdown review rewritten as that
    object and the model's own text kept beside it."""
    text = reply.get("text") or ""
    if extract_review_object(text) is not None:
        return reply
    review = markdown_review(text, known_paths)
    if review is None:
        return reply
    return {**reply, "text": json.dumps(review, indent=1, ensure_ascii=False), "original_text": text,
            "converted": {"from": "markdown", "by": MARKDOWN_VERSION}}


def reply_from_message(message, seconds: float | None = None, via: str = "direct", batch_id: str | None = None,
                       max_tokens: int | None = None) -> dict:
    """An API Message as the outcome table's reply, plus what is needed to trace it later."""
    usage = message.usage
    cached = (getattr(usage, "cache_creation_input_tokens", 0) or 0) + (getattr(usage, "cache_read_input_tokens", 0) or 0)
    refusal = None
    if message.stop_reason == "refusal":  # a safety classifier declined; scored as an empty review
        details = getattr(message, "stop_details", None)
        refusal = {"category": getattr(details, "category", None), "explanation": getattr(details, "explanation", None)}
    return {
        "text": "".join(block.text for block in message.content if block.type == "text"),
        "finish_reason": STOP_TO_FINISH.get(message.stop_reason, message.stop_reason),  # "length": cut off
        "stop_reason": message.stop_reason, "refusal": refusal,
        "prompt_tokens": usage.input_tokens + cached,
        "completion_tokens": usage.output_tokens,  # thinking included
        "seconds": seconds, "served_model": message.model, "message_id": message.id,
        "via": via, "batch_id": batch_id, "max_tokens": max_tokens, "received_at": now(),
    }


# --------------------------------------------------------------------------- calling the API

def make_client():
    try:
        import anthropic
    except ImportError:
        raise SystemExit("The Anthropic SDK is not installed: .venv/bin/python -m pip install anthropic") from None
    # Credentials come from the environment (ANTHROPIC_API_KEY, or an `ant auth login` profile).
    return anthropic.Anthropic(max_retries=5)


def run_concurrently(pending: dict[str, Callable[[], dict]], one: Callable[[str, dict], str], workers: int, log) -> dict:
    """Call `one(request id, params)` for every pending request, `workers` at a time, and count
    what it returns: "saved", "rejected" or "failed". `one` saves each reply itself as soon as it
    arrives. When the run has to stop (Ctrl+C, no credit left, a usage limit, a wrong key),
    nothing more is started and the requests already out are waited for, so no reply that was
    paid for is lost."""
    counts, stop = {"saved": 0, "rejected": 0, "failed": 0}, threading.Event()

    def guarded(rid: str, build: Callable[[], dict]) -> str:
        if stop.is_set():
            return "not sent"  # the run is stopping: a worker must not pick up one more request
        try:
            return one(rid, build())
        except BaseException:
            stop.set()
            raise

    pool, started = ThreadPoolExecutor(max_workers=max(1, workers)), time.monotonic()
    try:
        futures = [pool.submit(guarded, rid, build) for rid, build in pending.items()]
        for done, future in enumerate(as_completed(futures), 1):
            outcome = future.result()
            counts[outcome] = counts.get(outcome, 0) + 1
            if done % 10 == 0 or done == len(futures):
                left = (time.monotonic() - started) / done * (len(futures) - done)
                log(f"  {done}/{len(futures)} requests done: {counts}"
                    + (f"; about {left / 3600:.1f} hours left at this pace" if left >= 3600 else
                       f"; about {left / 60:.0f} minutes left at this pace" if left >= 60 else ""))
    except BaseException as error:
        pool.shutdown(wait=False, cancel_futures=True)  # nothing more is sent
        log(f"stopping ({type(error).__name__}) after {counts}: waiting for the requests already sent, so their "
            f"replies are saved and they are not paid for twice (Ctrl+C again abandons them)")
        pool.shutdown(wait=True)
        raise
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return counts


def fulfil_direct(client, pending: dict[str, Callable[[], dict]], store: ReplyStore, workers: int, log) -> dict:
    """Send each request to the API now, at standard prices. Requests are streamed so long ones
    do not hit HTTP timeouts. A rejected request is remembered; one that fails after the SDK's
    retries stays pending. An empty credit balance, or an authentication or permission error,
    stops the run."""
    import anthropic

    def one(rid: str, params: dict) -> str:
        started = time.monotonic()
        try:
            with client.messages.stream(**params) as stream:
                message = stream.get_final_message()
        except anthropic.RateLimitError as error:
            log(f"  request {rid[:12]} is still rate-limited after retries ({error}); it stays pending")
            return "failed"
        except anthropic.APIStatusError as error:
            if out_of_credits(str(error)):
                raise OutOfCredits(str(error)) from None  # not a fault of this request: do not mark it rejected
            if isinstance(error, anthropic.BadRequestError):
                store.reject(rid, "invalid_request_error", str(error))
                return "rejected"
            if error.status_code < 500:
                raise  # wrong key, no access to the model, unknown model: no later request would work
            log(f"  request {rid[:12]} failed with HTTP {error.status_code} after retries; it stays pending")
            return "failed"
        except anthropic.APIConnectionError as error:
            log(f"  request {rid[:12]} failed ({type(error).__name__}) after retries; it stays pending")
            return "failed"
        store.put(rid, reply_from_message(message, round(time.monotonic() - started, 2), max_tokens=params["max_tokens"]))
        return "saved"  # saved at once, so stopping later loses nothing that was answered

    return run_concurrently(pending, one, workers, log)


def chunks(pending: dict[str, Callable[[], dict]], max_bytes: int, max_requests: int = BATCH_MAX_REQUESTS):
    """Groups of (request id, params) that each stay under one batch's size limits."""
    group, size = [], 0
    for rid, build in pending.items():
        params = build()
        nbytes = len(json.dumps(params, ensure_ascii=False).encode("utf-8")) + 200
        if group and (size + nbytes > max_bytes or len(group) >= max_requests):
            yield group
            group, size = [], 0
        group.append((rid, params))
        size += nbytes
    if group:
        yield group


def submit_batches(client, pending: dict[str, Callable[[], dict]], state_dir: Path, kind: str, max_mb: float, log) -> list[str]:
    """Create Message Batches for the pending requests and record each under `state_dir`.

    A marker file is written before each upload and removed once the batch is recorded. Uploads
    are not retried automatically, because a retry after a lost response could create (and
    bill) a second batch. A marker left behind means the upload's outcome is unknown;
    `resolve_markers` settles it on the next run before anything is sent."""
    import anthropic
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    uploader, created = client.with_options(timeout=UPLOAD_TIMEOUT, max_retries=0), []
    for number, group in enumerate(chunks(pending, int(max_mb * 1e6))):
        rids, max_tokens = [rid for rid, _ in group], group[0][1]["max_tokens"]
        marker = state_dir / f"submitting-{int(time.time() * 1000)}-{number}.json"
        table.write_json(marker, {"kind": kind, "requests": rids, "max_tokens": max_tokens, "started_at": now()})
        try:
            batch = uploader.messages.batches.create(requests=[
                Request(custom_id=rid, params=MessageCreateParamsNonStreaming(**params)) for rid, params in group])
        except anthropic.APIStatusError as error:  # the API answered with an error, so no batch was created
            marker.unlink()
            if out_of_credits(str(error)):
                raise OutOfCredits(str(error)) from None
            raise SystemExit(f"The API refused the batch upload (HTTP {error.status_code}): {error}") from None
        table.write_json(state_dir / f"{batch.id}.json", {"id": batch.id, "kind": kind, "requests": rids,
                                                         "max_tokens": max_tokens, "created_at": now(), "collected": False})
        marker.unlink()
        created.append(batch.id)
        log(f"  created batch {batch.id} with {len(rids)} {kind} requests")
    return created


def batch_states(state_dir: Path) -> tuple[list[tuple[Path, dict]], list[Path]]:
    """(recorded batches, markers of uploads whose outcome is unknown)."""
    batches, markers = [], []
    for path in sorted(Path(state_dir).glob("*.json")):
        if path.name.startswith("submitting-"):
            markers.append(path)
        elif (state := table.read_json(path)) and state.get("id"):
            batches.append((path, state))
    return batches, markers


def in_flight(state_dir: Path) -> set[str]:
    """Request ids that are (or may be) in a batch whose results have not been collected."""
    batches, markers = batch_states(state_dir)
    states = [state for _, state in batches if not state.get("collected")] + [table.read_json(p) or {} for p in markers]
    return {rid for state in states for rid in state.get("requests") or []}


def resolve_markers(client, state_dir: Path, log, grace: float = UPLOAD_GRACE) -> None:
    """Settle every upload whose outcome is unknown: the process stopped, or the response was
    lost, between sending a batch and recording it. Its requests must be neither lost nor paid
    for twice, so the account's batches are checked:

    - Every batch that is not recorded here and was created while that upload could have been
      running is taken over. Its results are collected like any other batch's, and the upload's
      requests are not sent again while one of these batches is still open.
    - If no such batch shows up within `grace` seconds, the upload created none (the process
      that made it is gone, since only one works in a run folder), and its requests are free
      to be sent again."""
    batches, markers = batch_states(state_dir)
    if not markers:
        return
    known = {state["id"] for _, state in batches}
    deadline = time.monotonic() + grace
    while markers:
        recent = []
        for number, batch in enumerate(client.messages.batches.list(limit=100)):
            if number >= 300:
                break
            recent.append(batch)
        for marker in list(markers):
            state = table.read_json(marker) or {}
            started = datetime.fromisoformat(state["started_at"])
            found = [batch for batch in recent if batch.id not in known
                     and -CLOCK_SKEW <= (batch.created_at - started).total_seconds() <= UPLOAD_TIMEOUT + CLOCK_SKEW]
            old = (datetime.now(timezone.utc) - started).total_seconds() > UPLOAD_TIMEOUT + CLOCK_SKEW
            if not found and not old and time.monotonic() < deadline:
                continue  # too early to conclude that no batch was created
            for batch in found:
                table.write_json(state_dir / f"{batch.id}.json", {
                    "id": batch.id, "kind": state.get("kind"), "requests": state.get("requests"),
                    "max_tokens": state.get("max_tokens"), "created_at": batch.created_at.isoformat(),
                    "collected": False, "taken_over_from": marker.name})
                known.add(batch.id)
            marker.unlink()
            markers.remove(marker)
            log(f"  an upload started at {state['started_at']} was interrupted: "
                + (f"it had created {', '.join(batch.id for batch in found)}, now recorded; nothing is sent again"
                   if found else "the account has no batch from it, so its requests can be sent"))
        if markers:
            log(f"  checking whether an interrupted upload created a batch (up to {grace:g}s)")
            time.sleep(min(15, max(0.0, deadline - time.monotonic())))


def _error_of(result) -> tuple[str, str]:
    """(type, message) of a batch result that is not a success."""
    error = getattr(result, "error", None)
    data = error.to_dict() if hasattr(error, "to_dict") else error if isinstance(error, dict) else {}
    inner = data.get("error") if isinstance(data.get("error"), dict) else data
    return str(inner.get("type") or result.type), str(inner.get("message") or "")


def collect_batches(client, state_dir: Path, store: ReplyStore, log) -> dict:
    """Save the results of every finished batch not collected yet. `open` counts the batches
    still processing. Requests that errored on the server, expired or were canceled are not
    billed and simply stay pending; invalid requests are remembered as rejected. A batch is
    marked collected only after all its results are saved, so stopping midway loses nothing."""
    totals = {"open": 0, "saved": 0, "already_saved": 0, "rejected": 0, "failed": 0}
    for path, state in batch_states(state_dir)[0]:
        if state.get("collected"):
            continue
        batch = client.messages.batches.retrieve(state["id"])
        if batch.processing_status != "ended":
            totals["open"] += 1
            counts = batch.request_counts
            log(f"  batch {state['id']} is {batch.processing_status}: {counts.processing} processing, "
                f"{counts.succeeded} succeeded, {counts.errored} errored")
            continue
        counts = {"saved": 0, "already_saved": 0, "rejected": 0, "failed": 0}
        if getattr(batch, "results_url", "") is None:  # the API keeps results for 29 days after creation
            table.write_json(path, {**state, "collected": True, "collected_at": now(), "results_unavailable": True})
            log(f"  WARNING: batch {state['id']} ended but its results are no longer available (they are kept for "
                f"29 days). Requests without a saved reply would have to be sent, and paid for, again.")
            continue
        expected = set(state["requests"]) if state.get("taken_over_from") else None
        for item in client.messages.batches.results(state["id"]):  # any order: keyed by custom_id
            rid, result = item.custom_id, item.result
            if not REQUEST_ID.match(rid) or (expected is not None and rid not in expected):
                continue  # a batch that turned out not to be this run's
            if result.type == "succeeded":
                reply = reply_from_message(result.message, via="batch", batch_id=state["id"], max_tokens=state.get("max_tokens"))
                counts["saved" if store.put(rid, reply) else "already_saved"] += 1
            else:
                kind, message = _error_of(result)
                if out_of_credits(f"{kind} {message}"):
                    counts["failed"] += 1  # the credit ran out mid-batch: not billed, and not the request's fault
                    counts["no_credit"] = counts.get("no_credit", 0) + 1
                elif kind.startswith("invalid_request"):
                    store.reject(rid, kind, message)
                    counts["rejected"] += 1
                else:
                    counts["failed"] += 1
        table.write_json(path, {**state, "collected": True, "collected_at": now(), "counts": counts})
        log(f"  batch {state['id']} ended: {counts}")
        if counts.get("no_credit"):
            log(f"  {counts['no_credit']} of its requests were not processed because the API credit ran out. They were "
                f"not billed and are sent again on the next run, once credit is available.")
        for key, value in counts.items():
            totals[key] = totals.get(key, 0) + value
    return totals


def wait_for_batches(client, state_dir: Path, store: ReplyStore, log, poll_seconds: float) -> None:
    while collect_batches(client, state_dir, store, log)["open"]:
        time.sleep(poll_seconds)


# --------------------------------------------------------------------------- Claude Code on the plan's sign-in

def find_claude_code(explicit: str | None = None) -> str:
    """The Claude Code program: --claude-bin, else `claude` on PATH, else the newest build the
    Claude desktop app keeps for its own Code sessions (macOS)."""
    if explicit:
        if not Path(explicit).is_file():
            raise SystemExit(f"{explicit} does not exist")
        return explicit
    if found := shutil.which("claude"):
        return found

    def version(path: Path) -> tuple:
        return tuple(int(n) for n in re.findall(r"\d+", path.parts[len(DESKTOP_BUILDS.parts)])), path.stat().st_mtime

    builds = sorted(DESKTOP_BUILDS.glob("*/*/claude.app/Contents/MacOS/claude"), key=version)
    if not builds:
        raise SystemExit("Claude Code was not found: install it or the Claude desktop app, or pass --claude-bin.")
    return str(builds[-1])


def code_environment(max_tokens: int | None = None) -> dict:
    """The environment Claude Code runs in: this one, minus the variables that would change who
    pays or how it behaves. An exported API key would override the plan's sign-in and bill the
    API, and the CLAUDE_* variables of a Claude session this script is started from would make it
    a child of that session."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("ANTHROPIC_", "CLAUDE_", "CLAUDECODE"))
           or key == "CLAUDE_CODE_OAUTH_TOKEN"}  # a plan token from `claude setup-token`, for unattended runs
    if max_tokens:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_tokens)
    # Claude Code marks every request for its one-hour prompt cache. No request here is ever read
    # back from that cache, and writing to it counts twice the input (measured: 12.82 against about
    # 6.80 dollars at API rates for the first PR). This turns the marking off; the text sent is the same.
    env["DISABLE_PROMPT_CACHING"] = "1"
    return env


def code_version(binary: str) -> str:
    done = subprocess.run([binary, "--version"], capture_output=True, text=True, env=code_environment(), timeout=120)
    return done.stdout.strip() or "unknown version"


def code_auth(binary: str) -> dict:
    """Claude Code's sign-in state, from `claude auth status` (no model call)."""
    done = subprocess.run([binary, "auth", "status", "--json"], capture_output=True, text=True,
                          env=code_environment(), timeout=120)
    try:
        state = json.loads(done.stdout)
    except json.JSONDecodeError:
        return {}
    return state if isinstance(state, dict) else {}


def code_command(binary: str, params: dict) -> list[str]:
    """One request as a Claude Code print-mode command; the user message goes in on stdin.

    --system-prompt replaces Claude Code's own system prompt. It is the empty string when the
    request has none, as for the judge: leaving the option out would bring in Claude Code's
    agent prompt, and the API rejects a blank one. --tools "" removes every tool, so the model
    cannot look at anything but the request: not the repository, not the other levels, not the
    reference comments. --safe-mode keeps CLAUDE.md, memory, skills, plugins, hooks and MCP
    servers out, and nothing is saved as a session. What Claude Code still adds to a request is
    measured by `code_additions`. The output is a stream of JSON lines: the result, and a
    `rate_limit_event` that says how much of the plan's limits is used."""
    return [binary, "--print", "--model", params["model"], "--effort", params["output_config"]["effort"],
            "--system-prompt", params.get("system") or "", "--tools", "", "--output-format", "stream-json",
            "--verbose", "--safe-mode", "--no-session-persistence", "--disable-slash-commands", "--strict-mcp-config"]


def code_folder() -> Path:
    """The folder Claude Code runs in: empty, outside any repository, and the same every time,
    because Claude Code tells the model its working folder."""
    folder = Path(tempfile.gettempdir()) / "claude-outcome-table-cwd"
    folder.mkdir(exist_ok=True)
    if any(folder.iterdir()):
        raise SystemExit(f"{folder} must be empty (Claude Code could read what is in it); remove its contents")
    return folder


class _Capture(http.server.BaseHTTPRequestHandler):
    """Records request bodies and refuses the request. It never reads the headers, which carry the sign-in."""
    bodies: list

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        try:
            self.bodies.append(json.loads(raw))
        except json.JSONDecodeError:
            pass
        payload = b'{"type":"error","error":{"type":"invalid_request_error","message":"inspection only"}}'
        self.send_response(400)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def code_additions(binary: str, model: str = DEFAULT_MODEL, effort: str = DEFAULT_EFFORT) -> dict | None:
    """What Claude Code puts into a request besides the system prompt and user message it is
    given. Nothing is sent to Anthropic and no usage is spent: for this one call Claude Code is
    pointed at a server inside this process, which records the request and refuses it.

    Returns {"tools": [...], "texts": [(where, text), ...], "sha256": ...}, or None when the
    request could not be seen. The texts are normalized (email, working folder, date and version
    replaced by placeholders), so the hash changes only when Claude Code adds something else."""
    system_mark, user_mark = "OUTCOME-TABLE-SYSTEM-MARK", "OUTCOME-TABLE-USER-MARK"
    params = ClaudeSpec(model, effort, 1000, channel=VIA_CODE).params(
        [{"role": "system", "content": system_mark}, {"role": "user", "content": user_mark}])
    handler = type("Capture", (_Capture,), {"bodies": []})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    folder = code_folder()
    try:
        env = code_environment(params["max_tokens"])
        env["ANTHROPIC_BASE_URL"] = f"http://127.0.0.1:{server.server_address[1]}"
        subprocess.run(code_command(binary, params), input=user_mark, capture_output=True, text=True, cwd=folder,
                       env=env, timeout=180, start_new_session=True)
    except subprocess.TimeoutExpired:
        pass
    finally:
        server.shutdown()
        server.server_close()
    body = next((body for body in handler.bodies if isinstance(body, dict) and "messages" in body), None)
    if body is None:
        return None

    def normal(text: str) -> str:
        text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "<email>", text)
        for path in sorted({str(folder), str(folder.resolve())}, key=len, reverse=True):  # /private/var/... before /var/...
            text = text.replace(path, "<working folder>")
        text = re.sub(r"\b\d{4}-\d{2}-\d{2}\b", "<date>", text)
        return re.sub(r"cc_version=[^;\s]+", "cc_version=<version>", text).strip()

    texts = []
    system = body.get("system")
    for block in system if isinstance(system, list) else [{"text": system}] if system else []:
        if (text := block.get("text", "")) != system_mark:
            texts.append(("system prompt", normal(text)))
    for message in body["messages"]:
        content = message.get("content")
        for part in content if isinstance(content, list) else [{"type": "text", "text": content}]:
            text = part.get("text") if part.get("type") == "text" else json.dumps(part, sort_keys=True)
            if text != user_mark:
                texts.append((f"{message.get('role')} message", normal(text or "")))
    tools = [tool.get("name") for tool in body.get("tools") or []]
    digest = hashlib.sha256(json.dumps({"tools": tools, "texts": texts}, sort_keys=True).encode("utf-8")).hexdigest()
    return {"tools": tools, "texts": texts, "sha256": digest,
            "request": {key: body.get(key) for key in ("model", "max_tokens", "thinking", "output_config")}}


def describe_additions(additions: dict | None) -> list[str]:
    """Lines that say, for a person, what Claude Code adds to each request."""
    if additions is None:
        return ["could not see the request Claude Code builds, so what it adds to a request is unknown"]
    lines = [f"tools given to the model: {additions['tools'] or 'none'}",
             f"text added to each request: {sum(len(text) for _, text in additions['texts']):,} characters in "
             f"{len(additions['texts'])} pieces"]
    lines += [f"  in the {where}: {text[:150].replace(chr(10), ' ')}{'...' if len(text) > 150 else ''}"
              for where, text in additions["texts"]]
    return lines


def code_result(stdout: str) -> dict | None:
    """The result object in what Claude Code printed (`--output-format json` prints one object)."""
    candidates = [stdout] + list(reversed(stdout.strip().splitlines()))
    for text in candidates:
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            continue
        for item in reversed(value) if isinstance(value, list) else [value]:
            if isinstance(item, dict) and (item.get("type") == "result" or "result" in item):
                return item
    return None


def code_limits(stdout: str) -> dict | None:
    """The plan-limit report Claude Code printed with this call (the last `rate_limit_event`):
    status, the window that binds, when it resets, how much of each window is used, and whether
    paid extra usage is in use."""
    found = None
    for line in stdout.splitlines():
        if '"rate_limit_event"' in line:
            try:
                found = json.loads(line).get("rate_limit_info") or found
            except json.JSONDecodeError:
                pass
    return found


WINDOWS = {"five_hour": "5-hour", "seven_day": "week", "seven_day_opus": "week (Opus)", "seven_day_sonnet": "week (Sonnet)"}


def limits_text(limits: dict | None) -> str:
    parts = []
    for key, window in ((limits or {}).get("unifiedWindows") or {}).items():
        if window.get("utilization") is not None:
            reset = window.get("resetsAt")
            parts.append(f"{WINDOWS.get(key, key)} limit {window['utilization']:.0%} used"
                         + (f", resets {datetime.fromtimestamp(reset).strftime('%a %H:%M')}" if reset else ""))
    return "; ".join(parts) or "not reported"


def check_limits(limits: dict | None, caps: dict[str, float | None]) -> None:
    """Raise if the run must not send another request: paid extra usage is in use, or a window
    is used up to the cap this run was given."""
    if not limits:
        return
    if limits.get("isUsingOverage") or limits.get("rateLimitType") == "overage":
        raise ExtraUsage("this request was served from paid extra usage")
    extra_possible = limits.get("overageStatus") not in (None, "rejected")
    for key, window in (limits.get("unifiedWindows") or {}).items():
        cap = caps.get(key)
        if extra_possible:  # stop short of the limit, so that no request crosses into paid extra usage
            cap = min(cap if cap is not None else 100, 95)
        if cap is not None and window.get("utilization") is not None and window["utilization"] * 100 >= cap:
            raise LimitReached(f"the {WINDOWS.get(key, key)} limit is {window['utilization']:.0%} used, which reaches this "
                               f"run's cap of {cap:g}%", window.get("resetsAt"))


def call_claude_code(binary: str, params: dict, cwd: Path) -> dict:
    """Run one request through Claude Code and return it as a reply.

    Raises Rejected for a request that cannot be answered, LimitReached at the plan's usage
    limit, SystemExit when Claude Code is not signed in, and RuntimeError for anything else."""
    messages = params["messages"]
    if len(messages) != 1 or not isinstance(messages[0].get("content"), str):
        raise ValueError("a Claude Code request is one user message of text")
    started = time.monotonic()
    done = subprocess.run(code_command(binary, params), input=messages[0]["content"], capture_output=True, text=True,
                          cwd=cwd, env=code_environment(params["max_tokens"]), timeout=CODE_TIMEOUT,
                          start_new_session=True)  # so Ctrl+C here does not kill a call that is already out
    data, limits = code_result(done.stdout), code_limits(done.stdout)
    if data is None or data.get("is_error") or done.returncode != 0:
        message = str((data or {}).get("result") or done.stderr.strip() or done.stdout.strip()
                      or f"exit code {done.returncode}")[:1000]
        status = (data or {}).get("api_error_status")
        if status == 401 or re.search(r"failed to authenticate|not logged in|auth login|/login|invalid api key",
                                      message, re.IGNORECASE):
            raise SystemExit(f"{SIGN_IN_HELP}\n(Claude Code said: {message})")
        if re.search(r"prompt is too long|context window|too many tokens", message, re.IGNORECASE):
            raise Rejected(message)
        if (status == 429 or (limits or {}).get("status") == "rejected"
                or re.search(r"usage limit|rate limit|hit your|limit reached|spend limit|shared budget|quota|overloaded",
                             message, re.IGNORECASE)):
            raise LimitReached(message, (limits or {}).get("resetsAt"))
        raise RuntimeError(message)
    usage, models = data.get("usage") or {}, data.get("modelUsage") or {}
    stop = data.get("stop_reason") or "end_turn"
    return {
        "text": data.get("result") or "", "finish_reason": STOP_TO_FINISH.get(stop, stop), "stop_reason": stop,
        "refusal": {"category": None, "explanation": None} if stop == "refusal" else None,
        "prompt_tokens": sum(usage.get(key) or 0 for key in
                             ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")),
        "completion_tokens": usage.get("output_tokens") or 0,
        "seconds": round(time.monotonic() - started, 2),
        "served_model": max(models, key=lambda name: (models[name] or {}).get("outputTokens") or 0, default=params["model"]),
        "message_id": data.get("uuid") or data.get("session_id"), "via": VIA_CODE, "batch_id": None,
        "max_tokens": params["max_tokens"], "received_at": now(),
        "model_usage": models, "api_rate_equivalent_usd": data.get("total_cost_usd"), "limits": limits,
    }


def fulfil_code(binary: str, pending: dict[str, Callable[[], dict]], store: ReplyStore, workers: int, log,
                caps: dict[str, float | None] | None = None, find: Callable[[], str] | None = None,
                note: Callable[[dict], None] | None = None, until: float | None = None) -> dict:
    """Send each request through Claude Code's print mode, on the plan's sign-in. Each reply is
    saved as soon as it arrives. The run stops by raising LimitReached at the plan's usage limit
    or at one of `caps` (percent of a window), ExtraUsage if paid extra usage starts being used,
    and TooManyFailures after three failures in a row. Whatever stops it, nothing saved is lost."""
    cwd, program, state, lock = code_folder(), [binary], {"failures": 0, "saved": 0}, threading.Lock()

    def one(rid: str, params: dict) -> str:
        if until is not None and time.time() >= until:
            raise StopTime("the time given with --until has come")
        try:
            try:
                reply = call_claude_code(program[0], params, cwd)
            except FileNotFoundError:  # the desktop app replaced its Claude Code build during the run
                if find is None:
                    raise
                program[0] = find()
                log(f"  Claude Code moved; continuing with {program[0]}")
                reply = call_claude_code(program[0], params, cwd)
        except Rejected as error:
            store.reject(rid, "rejected_by_claude_code", str(error))
            return "rejected"
        except (LimitReached, StopTime):
            raise
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
            with lock:
                state["failures"] += 1
                if state["failures"] >= 3:
                    raise TooManyFailures(f"three requests in a row failed; the last error: {error}") from None
            log(f"  request {rid[:12]} failed ({str(error)[:200]}); it stays pending")
            return "failed"
        store.put(rid, reply)  # saved before anything below can stop the run
        with lock:
            state["failures"] = 0
            state["saved"] += 1
            if state["saved"] % 10 == 1:
                log(f"  plan limits: {limits_text(reply.get('limits'))}")
            if note and reply.get("limits"):
                note(reply["limits"])
        check_limits(reply.get("limits"), caps or {})
        return "saved"

    return run_concurrently(pending, one, workers, log)


def code_login(args) -> int:
    binary = find_claude_code(args.claude_bin)
    print(f"Starting Claude Code's sign-in ({binary}).\nIn the browser, sign in with the account that has your "
          f"Claude plan. Paste nothing into a chat.", flush=True)
    return subprocess.call([binary, "auth", "login", "--claudeai"], env=code_environment())


def code_check(args) -> int:
    """Before a long run: is Claude Code signed in with the plan, what does it add to a request
    (inspected locally, nothing sent), and do two tiny real calls come back from the right model."""
    binary = find_claude_code(args.claude_bin)
    auth = code_auth(binary)
    print(f"Claude Code: {binary} ({code_version(binary)})")
    print(f"signed in: {bool(auth.get('loggedIn'))}; method {auth.get('authMethod')}; plan {auth.get('subscriptionType')}")
    if not auth.get("loggedIn"):
        raise SystemExit(SIGN_IN_HELP)
    additions = code_additions(binary, args.model, args.effort)
    print("\n".join(describe_additions(additions)))
    clean = additions is not None and not additions["tools"] and sum(len(text) for _, text in additions["texts"]) <= 4000
    if not clean:
        print("WARNING: Claude Code gives the model tools, adds a lot of text, or could not be inspected. Do not build "
              "the table this way until that is understood.")
    spec, cwd = ClaudeSpec(args.model, args.effort, 2000, channel=VIA_CODE), code_folder()
    for label, messages in (
            ("with a system prompt, as for a review", [{"role": "system", "content": "Answer with the one word the user asks for."},
                                                       {"role": "user", "content": "Say: ready"}]),
            ("without a system prompt, as for a judge call", user_message("Answer with the one word: ready"))):
        reply = call_claude_code(binary, spec.params(messages), cwd)
        sent = sum(len(message["content"]) for message in messages)
        print(f"real call {label}: reply {reply['text'].strip()!r}; {reply['prompt_tokens']} input tokens for {sent} "
              f"characters of ours; {reply['completion_tokens']} output tokens; model {reply['served_model']}")
    limits = reply.get("limits") or {}
    print(f"plan limits now: {limits_text(limits)}")
    print("paid extra usage: " + ("IN USE" if limits.get("isUsingOverage") else
                                  f"not available ({limits.get('overageDisabledReason')})" if limits.get("overageStatus") == "rejected"
                                  else f"status {limits.get('overageStatus')}; a run stops before a limit so none is used"))
    print("Claude Code answers, gives the model no tools, and adds only the text listed above." if clean
          else "Check failed; see the warning above.")
    return 0 if clean else 1


# --------------------------------------------------------------------------- the run

def usd(amount: float) -> str:
    return f"${amount:,.2f}" if amount < 100 else f"${amount:,.0f}"


def tokens_text(tokens: float) -> str:
    return f"{tokens / 1e6:.1f}M" if tokens >= 1e6 else f"{tokens:,.0f}"


def judge_requests(reviews: list[dict], references: dict[str, dict], spec: ClaudeSpec) -> dict[str, str]:
    """Every judge prompt the saved reviews need, by request id. AACR's judge is called once per
    generated comment and no prompt depends on another answer, so all of them are known up front
    and can go into one batch."""
    prompts: dict[str, str] = {}

    def recorder(prompt: str) -> str:
        prompts.setdefault(spec.request_id(user_message(prompt)), prompt)
        return "[]"

    for review in reviews:
        if review.get("status") in SCORABLE:
            score_review(references[review["entry"]]["comments"], (review.get("parsed") or {}).get("findings", []), recorder)
    return prompts


class Run:
    """One run folder: the selected cells, the reviewer's settings and the saved replies."""

    def __init__(self, args):
        self.args = args
        self.spec = ClaudeSpec(args.model, args.effort, args.max_tokens, args.replicate, channel=args.via)
        self.via_code = args.via == VIA_CODE
        if args.workers is None:
            args.workers = 1 if self.via_code else 4
        name = args.run_name or (("claude-code_" if self.via_code else "") + f"{args.model}_{args.effort}_codex-rubric_v1"
                                 + (f"_rep{args.replicate}" if args.replicate else ""))
        self.out = self._path(args.out_root) / name
        self.contexts, self.dataset = self._path(args.contexts), self._path(args.dataset)
        self.store, self.batches = ReplyStore(self.out / "api"), self.out / "api" / "batches"
        self.limit, self.rubric = args.max_input_tokens, load_rubric()  # refuses an edited rubric file
        if not self.contexts.is_file():
            raise SystemExit(f"{self.contexts} is missing; build it with scripts/dataset/build_dataset.py data/aacr")
        cells = table.discover_cells(self.contexts, None, args.levels)
        entries = list(dict.fromkeys(cell.entry for cell in cells))  # dataset order
        if args.entries:
            unknown = sorted(set(args.entries) - set(entries))
            if unknown:
                raise SystemExit(f"not in {self.contexts.name}: {', '.join(unknown)}")
            entries = [entry for entry in entries if entry in set(args.entries)]
        if args.first:
            entries = entries[:args.first]
        self.cells = [cell for cell in cells if cell.entry in set(entries)]
        self.entries = entries
        self._requests: dict | None = None
        self.claude_code: dict | None = None  # what the Claude Code pre-flight found, for the manifest

    @staticmethod
    def _path(value: str) -> Path:
        return Path(value) if Path(value).is_absolute() else PROJECT_ROOT / value

    def log(self, text: str) -> None:
        print(text, flush=True)
        if self.out.is_dir():
            with (self.out / "claude_outcome_table.log").open("a", encoding="utf-8") as handle:
                handle.write(f"{now()} {text}\n")

    def review_requests(self) -> dict:
        """request id -> the first cell with that input. Identical levels share one request, and
        cells above the input limit are left out (the table records them as too_long)."""
        if self._requests is None:
            self._requests = {}
            for cell in self.cells:
                if self.limit is None or (cell.input_tokens or 0) <= self.limit:
                    self._requests.setdefault(self.spec.request_id(review_messages(cell.text, self.rubric)), cell)
        return self._requests

    def review_params(self, rid: str) -> dict:
        return self.spec.params(review_messages(self.review_requests()[rid].text, self.rubric))

    def review_tokens(self, rids) -> tuple[int, int]:
        """(input tokens, how many of the requests have an exact count from the API)."""
        total = counted = 0
        for rid in rids:
            exact = self.store.token_count(rid)
            counted += exact is not None
            total += exact if exact is not None else (self.review_requests()[rid].input_tokens or 0) + PROMPT_TOKENS
        return total, counted

    def prices(self, model: str) -> tuple[float, float] | None:
        if self.args.price_in is not None and self.args.price_out is not None:
            return self.args.price_in, self.args.price_out
        return PRICES.get(model)

    def cost_text(self, model: str, input_tokens: float, output_tokens: float | None, exact: bool) -> str:
        if self.via_code:
            return "no API charge: these calls count against your Claude plan's usage limits"
        prices = self.prices(model)
        if prices is None:
            return f"no price known for {model}; pass --price-in and --price-out (USD per million tokens)"
        cost_in = input_tokens / 1e6 * prices[0]
        text = (f"input {'cost' if exact else 'cost, estimated'}: {usd(cost_in)} direct, {usd(cost_in * BATCH_DISCOUNT)} batch")
        if output_tokens is not None:
            cost_out = output_tokens / 1e6 * prices[1]
            text += f"; output: about {usd(cost_out)} direct, {usd(cost_out * BATCH_DISCOUNT)} batch"
        return text + f" (${prices[0]:g} in / ${prices[1]:g} out per million tokens, prices of {PRICES_AS_OF})"

    def mean_output_tokens(self, rids) -> float | None:
        """Mean output tokens of the saved replies among `rids`, once there are a few to go by."""
        seen = [reply["completion_tokens"] for rid in rids if (reply := self.store.get(rid))]
        return sum(seen) / len(seen) if len(seen) >= 5 else None

    def changes_since_last_run(self) -> list[str]:
        """Differences from the settings this run folder's replies were made with. Each one means
        new requests, so it is shown before anything is paid for."""
        manifest = table.read_json(self.out / "manifest.json")
        if not manifest or not (manifest.get("reviewer_api") or {}).get("replies"):
            return []
        from src.tracer_context.aacr import file_sha256

        changes, before = [], manifest.get("reviewer_identity") or {}
        for key in ("kind", "model", "effort", "replicate"):
            if before.get(key) != self.spec.identity[key]:
                changes.append(f"this folder's replies were made with {key} {before.get(key)!r}, not "
                               f"{self.spec.identity[key]!r}; none of them can be reused")
        if manifest.get("prompt_sha256") != prompt_sha256(self.rubric):
            changes.append("the review prompt changed since this folder's replies were made; none of them can be reused")
        if manifest.get("contexts_sha256") != file_sha256(self.contexts):
            changes.append(f"{self.contexts.name} changed since this folder's replies were made; every cell whose "
                           f"text changed needs a new review")
        return changes

    @contextmanager
    def locked(self):
        """Only one process may send or collect in a run folder: two would each see the same
        requests as unsent and pay for them twice."""
        (self.out / "api").mkdir(parents=True, exist_ok=True)
        handle = (self.out / "api" / ".lock").open("w")
        try:
            try:
                import fcntl
            except ImportError:  # Windows: no lock; do not start two runs in one folder
                yield
                return
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise SystemExit(f"Another claude_outcome_table.py process is working in {self.out}. Wait for it "
                                 f"to finish, so that nothing is sent twice.") from None
            yield
        finally:
            handle.close()  # also releases the lock

    def write_reviews(self) -> list[dict]:
        """Turn the saved replies into the pipeline's review records (reviews/<entry>/<level>.json).
        It sends nothing, so it is safe at any time, also while another process is reviewing."""
        known = {rid: cell.known_paths for rid, cell in self.review_requests().items()}
        return table.run_reviews(self.cells, ClaudeChat(self.spec, self.store, known), self.rubric, self.out,
                                 max_input_tokens=self.limit)

    def write_manifest(self, judge_identity=None) -> None:
        manifest = table.read_json(self.out / "manifest.json") or {}
        from src.tracer_context.aacr import file_sha256

        replies = [reply for rid in self.review_requests() if (reply := self.store.get(rid))]
        dates = sorted(reply["received_at"] for reply in replies)
        config = {key: value for key, value in vars(self.args).items() if key != "handler"}
        manifest.update({
            "profile": f"claude via {self.spec.channel} ({SCRIPT})", "config": config,
            "written_at": now(),
            "code_commit": subprocess.run(["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
                                          capture_output=True, text=True).stdout.strip(),
            "rubric_sha256": RUBRIC_SHA256, "review_request": REVIEW_REQUEST,
            "prompt_sha256": prompt_sha256(self.rubric), "review_prompt_version": REVIEW_PROMPT_VERSION,
            "judge_prompt_version": JUDGE_PROMPT_VERSION, "parser_version": PARSER_VERSION,
            "reviewer_identity": self.spec.identity,
            "contexts": str(self.contexts), "contexts_sha256": file_sha256(self.contexts),
            "reviewer_api": {  # API models are retired eventually: keep the served model and the dates
                "served_models": sorted({reply["served_model"] for reply in replies}),
                "first_reply": dates[0] if dates else None, "last_reply": dates[-1] if dates else None,
                "replies": len(replies), "input_tokens": sum(reply["prompt_tokens"] for reply in replies),
                "output_tokens": sum(reply["completion_tokens"] for reply in replies),
                "batches": sorted({reply["batch_id"] for reply in replies if reply.get("batch_id")}),
            },
            # Replies that are not the rubric's JSON object, and how many of them were a Markdown review.
            "markdown_replies": {
                "converter": MARKDOWN_VERSION,
                "not_json": sum(1 for reply in replies if extract_review_object(reply["text"]) is None),
                "read_as_markdown": sum(1 for reply in replies if as_json_review(reply).get("converted")),
            },
        })
        if self.dataset.is_file():
            manifest.update(dataset=str(self.dataset), dataset_sha256=file_sha256(self.dataset))
        if judge_identity is not None:
            manifest["judge_identity"] = judge_identity
        if self.claude_code:
            manifest["claude_code"] = self.claude_code
        table.write_json(self.out / "manifest.json", manifest)


def confirm(args, question: str) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        raise SystemExit("This step spends money and needs a yes: re-run it in a terminal, or pass --yes.")
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def fulfil(run: Run, needed: dict[str, Callable[[], dict]], kind: str, describe: Callable[[list[str]], str],
           client_factory) -> bool:
    """Get a saved reply for every needed request: collect finished batches, then send what is
    still missing, after showing the cost and asking. True when no request is left waiting."""
    args, store = run.args, run.store
    max_tokens = run.spec.max_tokens if kind == "review" else args.judge_max_tokens

    def missing() -> list[str]:
        return [rid for rid in needed
                if not store.answered(rid, max_tokens) and (args.retry_rejected or not store.rejection(rid))]

    run.batches.mkdir(parents=True, exist_ok=True)
    if not missing():
        run.log(f"{kind}: all {len(needed)} requests already have a saved reply; nothing to send")
        return True
    if run.via_code:
        return fulfil_through_code(run, needed, kind, describe, missing)
    client = client_factory()
    resolve_markers(client, run.batches, run.log, args.upload_grace)  # before anything can be sent twice
    if any(not state.get("collected") for _, state in batch_states(run.batches)[0]):
        collect_batches(client, run.batches, store, run.log)
    waiting = in_flight(run.batches)
    to_send = [rid for rid in missing() if rid not in waiting]
    run.log(f"{kind}: {len(needed)} requests, {len(needed) - len(missing())} answered, "
            f"{len(missing()) - len(to_send)} in a running batch, {len(to_send)} to send")
    if to_send:
        for warning in run.changes_since_last_run() if kind == "review" else []:
            run.log(f"  WARNING: {warning}")
        run.log(f"  {describe(to_send)}")
        how = ("as Message Batches (half price; most finish within an hour, at most 24 hours)" if args.mode == "batch"
               else f"directly, {args.workers} at a time, at standard prices")
        if not confirm(args, f"Send {len(to_send)} {kind} requests to {run_model(run, kind)} {how}?"):
            run.log("nothing sent")
            return False
        pending = {rid: needed[rid] for rid in to_send}
        try:
            if args.mode == "batch":
                submit_batches(client, pending, run.batches, kind, args.batch_mb, run.log)
            else:
                run.log(f"  direct: {fulfil_direct(client, pending, store, args.workers, run.log)}")
        except OutOfCredits as error:
            run.log(f"The API credit is used up ({error}). Nothing is lost: every reply that arrived is saved. Run "
                    f"the same command again once credit is available; only the missing requests are sent.")
            return False
    if any(not state.get("collected") for _, state in batch_states(run.batches)[0]):
        if args.no_wait:
            run.log("batches are processing; run the same command again later to collect them")
            return False
        run.log(f"waiting for the batches (checking every {args.poll_seconds:g}s; Ctrl+C stops waiting, "
                f"not the batches)")
        wait_for_batches(client, run.batches, store, run.log, args.poll_seconds)
    left = missing()
    if left:
        run.log(f"{kind}: {len(left)} requests still have no reply (failed or expired); run the command again")
    return not left


def fulfil_through_code(run: Run, needed: dict[str, Callable[[], dict]], kind: str,
                        describe: Callable[[list[str]], str], missing: Callable[[], list[str]]) -> bool:
    """The Claude Code branch of `fulfil`: no batches, no API key, the plan's usage limits."""
    args, binary = run.args, find_claude_code(run.args.claude_bin)
    auth = code_auth(binary)
    if not auth.get("loggedIn"):
        raise SystemExit(SIGN_IN_HELP)
    to_send = missing()
    run.log(f"{kind}: {len(needed)} requests, {len(needed) - len(to_send)} answered, {len(to_send)} to send")
    run.log(f"  through Claude Code {code_version(binary)}; signed in with {auth.get('authMethod')}, plan "
            f"{auth.get('subscriptionType')}")
    if not auth.get("subscriptionType"):
        run.log("  WARNING: this sign-in shows no Claude plan, so these calls may be billed to an API account instead")
    additions = code_additions(binary, run_model(run, kind),
                               run.spec.effort if kind == "review" else args.judge_effort)
    for line in describe_additions(additions)[:2]:
        run.log(f"  {line}")
    if additions and additions["tools"]:
        raise SystemExit("Claude Code would give the model tools, so it could read more than the request. Not sending.")
    before = ((table.read_json(run.out / "manifest.json") or {}).get("claude_code") or {}).get("additions_sha256")
    if additions and before and before != additions["sha256"]:
        run.log("  WARNING: Claude Code now adds different text to a request than when this folder's earlier replies "
                "were made (it was probably updated). `code-check` shows the text; the two sets of replies differ in it.")
    run.claude_code = {"version": code_version(binary), "plan": auth.get("subscriptionType"),
                       "additions_sha256": additions["sha256"] if additions else None,
                       "additions": additions["texts"] if additions else None}
    for warning in run.changes_since_last_run() if kind == "review" else []:
        run.log(f"  WARNING: {warning}")
    run.log(f"  {describe(to_send)}")
    caps, until = {"five_hour": args.five_hour_cap, "seven_day": args.week_cap}, parse_until(args.until)
    run.log(f"  this run stops sending when the week limit is {args.week_cap:g}% used"
            + (f" or the 5-hour limit {args.five_hour_cap:g}% used" if args.five_hour_cap is not None else "")
            + ("; at a limit it waits for the reset and continues" if args.wait_on_limit else
               "; at a limit it stops, and the same command continues later")
            + (f"; it sends nothing after {datetime.fromtimestamp(until).strftime('%a %H:%M')}" if until else ""))
    if not confirm(args, f"Send {len(to_send)} {kind} requests to {run_model(run, kind)} through Claude Code, "
                         f"{args.workers} at a time? They use your plan's limits, which this window shares."):
        run.log("nothing sent")
        return False
    limits_file = run.out / "api" / "limits.json"

    def remember(limits: dict) -> None:
        table.write_json(limits_file, {"seen_at": now(), "limits": limits})

    def remembered() -> dict | None:
        """The last limit report, for the windows that have not reset since it was made."""
        seen = (table.read_json(limits_file) or {}).get("limits") or {}
        windows = {key: window for key, window in (seen.get("unifiedWindows") or {}).items()
                   if (window.get("resetsAt") or 0) > time.time()}
        return {"overageStatus": seen.get("overageStatus"), "unifiedWindows": windows} if windows else None

    while to_send:
        try:
            check_limits(remembered(), caps)  # already at a cap: do not send even one request
            counts = fulfil_code(binary, {rid: needed[rid] for rid in to_send}, run.store, args.workers, run.log, caps,
                                 lambda: find_claude_code(args.claude_bin), remember, until)
            run.log(f"  claude code: {counts}")
            break
        except StopTime as error:
            run.log(f"STOPPED: {error}. Every reply that arrived is saved; {len(missing())} {kind} requests are left. "
                    f"Run the command again to continue.")
            return False
        except ExtraUsage as error:
            run.log(f"STOPPED: {error}. Nothing more is sent, so nothing more is billed. Every reply that arrived is "
                    f"saved. Turn extra usage off on claude.ai, or wait for the plan's limit to reset, then run the "
                    f"same command again.")
            return False
        except TooManyFailures as error:
            run.log(f"STOPPED: {error}. Every reply that arrived is saved; run the same command again to continue.")
            return False
        except LimitReached as error:
            to_send = missing()
            when = datetime.fromtimestamp(error.reset).strftime("%a %H:%M") if error.reset else "an unknown time"
            run.log(f"LIMIT: {error}. It resets at {when}. Every reply that arrived is saved; {len(to_send)} {kind} "
                    f"requests are left.")
            if not to_send:
                break
            if not args.wait_on_limit:
                run.log("Run the same command again after the reset; only the missing requests are sent.")
                return False
            deadline = (error.reset + args.limit_margin_seconds if error.reset
                        else time.time() + args.limit_wait_minutes * 60)
            deadline = max(deadline, time.time() + min(args.limit_margin_seconds, 60))
            if until is not None and deadline >= until:
                run.log(f"The reset comes after the time given with --until, so the run stops here. Run the command "
                        f"again to continue.")
                return False
            run.log(f"waiting until {datetime.fromtimestamp(deadline).strftime('%a %H:%M:%S')} to continue "
                    f"(Ctrl+C stops the run; nothing is lost)")
            while time.time() < deadline:  # short sleeps, so a laptop that slept continues promptly after waking
                time.sleep(max(0.0, min(60, deadline - time.time())))
            to_send = missing()
    left = missing()
    if left:
        run.log(f"{kind}: {len(left)} requests still have no reply; run the command again")
    return not left


def run_model(run: Run, kind: str) -> str:
    return run.spec.model if kind == "review" else (run.args.judge_model or DEFAULT_MODEL)


# --------------------------------------------------------------------------- commands

def describe_selection(run: Run) -> None:
    requests = run.review_requests()
    too_long = sum(1 for cell in run.cells if run.limit is not None and (cell.input_tokens or 0) > run.limit)
    run.log(f"run folder: {run.out}")
    run.log(f"reviewer: {run.spec.model}, effort {run.spec.effort}, max_tokens {run.spec.max_tokens:,}"
            + (f", replicate {run.spec.replicate}" if run.spec.replicate else "")
            + (", through Claude Code on your plan's sign-in" if run.via_code else ", through the Claude API"))
    run.log(f"cells: {len(run.cells)} from {len(run.entries)} PRs; {too_long} over {run.limit:,} input tokens "
            f"(not sent); {len(run.cells) - too_long - len(requests)} identical to another level (reviewed once); "
            f"{len(requests)} requests")


def cmd_plan(run: Run, client_factory) -> int:
    describe_selection(run)
    requests, store = run.review_requests(), run.store
    answered = [rid for rid in requests if store.answered(rid, run.spec.max_tokens)]
    rejected = [rid for rid in requests if rid not in set(answered) and store.rejection(rid)]
    waiting = in_flight(run.batches) & set(requests) - set(answered)
    to_send = [rid for rid in requests if rid not in set(answered) | set(rejected) | waiting]
    run.log(f"requests: {len(answered)} answered (never sent again), {len(rejected)} rejected by the API, "
            f"{len(waiting)} in a running batch, {len(to_send)} to send")
    for warning in run.changes_since_last_run():
        run.log(f"WARNING: {warning}")
    for label, rids in (("all requests", list(requests)), ("still to send", to_send)):
        if label == "still to send" and len(to_send) in (0, len(requests)):
            continue  # nothing left, or the same numbers again
        tokens, counted = run.review_tokens(rids)
        source = ("counted by the API" if counted == len(rids) else
                  "by the dataset's own count; Claude's tokenizer counts differently, `count` measures it"
                  if not counted else f"{counted} of {len(rids)} counted by the API, the rest by the dataset's count")
        mean_out = run.mean_output_tokens(requests)
        run.log(f"{label}: {tokens_text(tokens)} input tokens ({source})")
        run.log(f"  {run.cost_text(run.spec.model, tokens, mean_out * len(rids) if mean_out else None, counted == len(rids))}")
    if run.mean_output_tokens(requests) is None and not run.via_code:
        run.log("output (thinking plus the review) is billed on top; a pilot shows how much it is per review")
    return 0


def cmd_count(run: Run, client_factory) -> int:
    if run.via_code:
        raise SystemExit("`count` asks the Claude API, which --via claude-code does not use. `plan` shows the "
                         "dataset's own token counts.")
    describe_selection(run)
    requests = run.review_requests()
    todo = [rid for rid in requests if run.store.token_count(rid) is None]
    if todo:
        client = client_factory()

        def one(rid: str) -> None:
            params = run.review_params(rid)
            run.store.put_token_count(rid, client.messages.count_tokens(
                model=params["model"], system=params["system"], messages=params["messages"]).input_tokens)

        run.log(f"counting input tokens for {len(todo)} requests with the API (free, rate-limited)")
        with ThreadPoolExecutor(max_workers=max(1, run.args.workers)) as pool:
            for done, _ in enumerate(pool.map(one, todo), 1):
                if done % 100 == 0 or done == len(todo):
                    run.log(f"  {done}/{len(todo)}")
    tokens, _ = run.review_tokens(requests)
    dataset = sum((cell.input_tokens or 0) + PROMPT_TOKENS for cell in requests.values())
    if not requests:
        return 0
    run.log(f"{len(requests)} requests: {tokens:,} input tokens for {run.spec.model} "
            f"({tokens / dataset:.2f}x the dataset's own count of {dataset:,}); "
            f"largest request {max(run.store.token_count(rid) for rid in requests):,}")
    run.log(f"  {run.cost_text(run.spec.model, tokens, None, True)}")
    return 0


def cmd_review(run: Run, client_factory) -> int:
    run.out.mkdir(parents=True, exist_ok=True)
    describe_selection(run)
    requests = run.review_requests()

    def describe(rids: list[str]) -> str:
        tokens, counted = run.review_tokens(rids)
        mean_out = run.mean_output_tokens(requests)
        return (f"{tokens_text(tokens)} input tokens; "
                + run.cost_text(run.spec.model, tokens, mean_out * len(rids) if mean_out else None, counted == len(rids))
                + ("" if mean_out or run.via_code else "; output (thinking plus the review) is billed on top"))

    done = fulfil(run, {rid: (lambda rid=rid: run.review_params(rid)) for rid in requests}, "review", describe,
                  client_factory)
    reviews = run.write_reviews()
    run.write_manifest()
    report_reviews(run, reviews)
    return 0 if done else 2


def report_reviews(run: Run, reviews: list[dict]) -> None:
    statuses: dict[str, int] = {}
    for review in reviews:
        statuses[review["status"]] = statuses.get(review["status"], 0) + 1
    replies = [review["reply"] for review in reviews if review.get("reply") and not review.get("reused_from")]
    run.log(f"review records: {statuses} (an `error` cell has no reply yet or was rejected; see its file)")
    if replies:
        tokens_in, tokens_out = (sum(reply[key] for reply in replies) for key in ("prompt_tokens", "completion_tokens"))
        refused = sum(1 for reply in replies if reply.get("stop_reason") == "refusal")
        cut = sum(1 for reply in replies if reply.get("finish_reason") == "length")
        run.log(f"  {len(replies)} replies: {tokens_in:,} input and {tokens_out:,} output tokens "
                f"({tokens_out / len(replies):,.0f} output per review); {refused} refused, {cut} cut off at max_tokens; "
                f"served by {sorted({reply.get('served_model') for reply in replies})}")
        converted = sum(1 for reply in replies if reply.get("converted"))
        unread = sum(1 for review in reviews if review["status"] == "malformed" and not review.get("reused_from"))
        if converted or unread:
            run.log(f"  {converted} replies were a review written as Markdown, not the rubric's JSON, and were read by "
                    f"{MARKDOWN_VERSION}; {unread} replies could not be read (malformed: scored as an empty review)")
        if cut:
            run.log(f"  to redo only the {cut} cut-off reviews, run review again with a higher --max-tokens "
                    f"(now {run.spec.max_tokens:,}); the complete ones are not sent again")
        prices = run.prices(run.spec.model)
        if run.via_code:
            notional = sum(reply.get("api_rate_equivalent_usd") or 0 for reply in replies)
            run.log(f"  not charged on a plan sign-in; Claude Code reports {usd(notional)} at API rates for these replies")
        elif prices:
            spent = sum((reply["prompt_tokens"] * prices[0] + reply["completion_tokens"] * prices[1]) / 1e6
                        * (BATCH_DISCOUNT if reply.get("via") == "batch" else 1) for reply in replies)
            run.log(f"  cost of these replies at the listed prices: {usd(spent)}")


def make_judge(run: Run):
    """(judge callable, its ClaudeSpec or None)."""
    args = run.args
    if args.judge == "keyword-test":
        run.log("WARNING: keyword-test is a stand-in judge for pipeline tests; its scores are not meaningful")
        return backends.KeywordOverlapJudge(), None
    if args.judge == "openai":  # the project's own client for an OpenAI-compatible server, e.g. vLLM with an open model
        if not (args.judge_base_url and args.judge_model):
            raise SystemExit("--judge openai needs --judge-base-url and --judge-model")
        chat = backends.OpenAIChat(args.judge_base_url, args.judge_model, max_tokens=args.judge_max_tokens,
                                   api_key=os.environ.get(args.judge_api_key_env or ""), enable_thinking=None)
        return backends.judge_caller(chat), None
    spec = ClaudeSpec(args.judge_model or DEFAULT_MODEL, args.judge_effort, args.judge_max_tokens,
                      channel=run.spec.channel)
    run.log("NOTE: the judge is a Claude model like the reviewer. The roadmap (Section 8.5.2) asks for a judge "
            "from another model family, or at least a reported correlation with one.")
    return backends.judge_caller(ClaudeChat(spec, run.store)), spec


def cmd_judge(run: Run, client_factory) -> int:
    if not run.args.judge:
        raise SystemExit("Choose the judge: --judge claude, --judge openai (with --judge-base-url and "
                         "--judge-model) or --judge keyword-test. The project's judge model is still undecided.")
    if not run.dataset.is_file():
        raise SystemExit(f"{run.dataset} (AACR-Bench's reference comments) is missing")
    run.out.mkdir(parents=True, exist_ok=True)
    reviews = run.write_reviews()
    references = table.references_by_entry(run.dataset)
    judge, spec = make_judge(run)
    if spec is not None:
        prompts = judge_requests(reviews, references, spec)

        def describe(rids: list[str]) -> str:
            tokens = sum(len(prompts[rid]) for rid in rids) / 4
            return (f"about {tokens_text(tokens)} input tokens (characters / 4); "
                    + run.cost_text(spec.model, tokens, None, False) + ("" if run.via_code else "; output is billed on top"))

        needed = {rid: (lambda prompt=prompt: spec.params(user_message(prompt))) for rid, prompt in prompts.items()}
        if not fulfil(run, needed, "judge", describe, client_factory):
            return 2
        cut = sum(1 for rid in prompts if (run.store.get(rid) or {}).get("finish_reason") == "length")
        if cut:
            run.log(f"{cut} judge replies were cut off at max_tokens; a higher --judge-max-tokens redoes only those")
    judgments = table.run_judgments(reviews, references, judge, run.out)
    run.log(f"judged {len(judgments)} reviews; {sum(j['judge_errors'] for j in judgments)} judge errors "
            f"(re-run to retry them)")
    run.write_manifest(judge_identity=getattr(judge, "identity", None))
    return write_table(run, reviews, references)


def load_judgments(run: Run, reviews: list[dict]) -> list[dict]:
    """The saved judgment of each review, if it was made for the review as it is now."""
    judgments = []
    for review in reviews:
        saved = table.read_json(run.out / "judgments" / review["entry"] / f"{review['level']}.json")
        if saved and saved.get("review_key") == review.get("key"):
            judgments.append(saved)
    return judgments


def write_table(run: Run, reviews: list[dict], references: dict[str, dict]) -> int:
    judgments = load_judgments(run, reviews)
    judges = {json.dumps(j.get("judge"), sort_keys=True, default=str) for j in judgments}
    if len(judges) > 1:
        run.log(f"WARNING: the saved judgments come from {len(judges)} different judges; re-run the judge step "
                f"so that one judge scores every review")
    rows, coverage = table.build_table(reviews, judgments, references)
    table.write_outputs(run.out, rows, coverage)
    run.log(f"wrote {run.out / 'table.csv'} ({len(rows)} rows, {len(judgments)} judged) and coverage.csv")
    for row in table.summarize_by_level(rows):
        run.log(f"  {row['level']:16s} cells {row['cells']:4d}  judged {row['judged']:4d}  "
                f"recall {row['recall_pooled']:.3f}  precision {row['precision_pooled']:.3f}  "
                f"balanced {row['balanced_pooled']:.3f}  {row['statuses']}")
    return 0


def cmd_table(run: Run, client_factory) -> int:
    if not run.out.is_dir():
        raise SystemExit(f"{run.out} does not exist yet; run the review step first")
    if not run.dataset.is_file():
        raise SystemExit(f"{run.dataset} (AACR-Bench's reference comments) is missing")
    reviews = run.write_reviews()
    report_reviews(run, reviews)
    return write_table(run, reviews, table.references_by_entry(run.dataset))


def cmd_run(run: Run, client_factory) -> int:
    if not run.args.judge:
        raise SystemExit("`run` needs the judge too: --judge claude, --judge openai or --judge keyword-test")
    return cmd_review(run, client_factory) or cmd_judge(run, client_factory)


def cmd_status(run: Run, client_factory) -> int:
    describe_selection(run)
    batches, markers = batch_states(run.batches)
    for _, state in batches:
        run.log(f"batch {state['id']} ({state.get('kind')}, {len(state.get('requests') or [])} requests, created "
                f"{state.get('created_at')}): " + (f"collected {state.get('counts')}" if state.get("collected") else
                                                    "not collected yet; run review, judge or collect within 29 days "
                                                    "of its creation, after that its results are gone"))
    for marker in markers:
        run.log(f"interrupted upload, settled automatically by the next review, judge or collect: {marker.name}")
    requests = run.review_requests()
    run.log(f"review replies saved: {sum(run.store.answered(rid, run.spec.max_tokens) for rid in requests)} "
            f"of {len(requests)}")
    seen = table.read_json(run.out / "api" / "limits.json")
    if seen:
        run.log(f"plan limits when last seen ({seen['seen_at']}): {limits_text(seen['limits'])}")
    if run.args.account:
        run.log("the account's most recent batches:")
        for number, batch in enumerate(client_factory().messages.batches.list(limit=20)):
            if number == 20:
                break
            run.log(f"  {batch.id} {batch.processing_status} created {batch.created_at} {batch.request_counts}")
    return 0


def cmd_collect(run: Run, client_factory) -> int:
    run.batches.mkdir(parents=True, exist_ok=True)
    client = client_factory()
    resolve_markers(client, run.batches, run.log, run.args.upload_grace)
    if run.args.adopt:  # a batch this folder has no record of, e.g. after the folder was lost
        table.write_json(run.batches / f"{run.args.adopt}.json", {"id": run.args.adopt, "kind": "adopted",
                                                                 "requests": None, "created_at": None, "collected": False})
    if run.args.again:  # read every recorded batch's results again: free, and restores deleted replies
        for path, state in batch_states(run.batches)[0]:
            table.write_json(path, {**state, "collected": False})
    totals = collect_batches(client, run.batches, run.store, run.log)
    run.log(f"collected: {totals}")
    return 0


COMMANDS = {"plan": cmd_plan, "count": cmd_count, "review": cmd_review, "judge": cmd_judge, "table": cmd_table,
            "run": cmd_run, "status": cmd_status, "collect": cmd_collect}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    group = common.add_argument_group("reviewer")
    group.add_argument("--via", default=VIA_API, choices=(VIA_API, VIA_CODE),
                       help="api: the Claude API, with a key or `ant auth login` (default). claude-code: Claude "
                            "Code's print mode on your Claude plan's sign-in, with no API key and its own run folder")
    group.add_argument("--claude-bin", help="the Claude Code program for --via claude-code (default: `claude` on "
                                            "PATH, else the Claude desktop app's)")
    group.add_argument("--model", default=DEFAULT_MODEL, help=f"reviewer model id (default {DEFAULT_MODEL})")
    group.add_argument("--effort", default=DEFAULT_EFFORT, choices=EFFORTS, help=f"effort level (default {DEFAULT_EFFORT})")
    group.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS,
                       help=f"output limit per review, thinking included (default {DEFAULT_MAX_TOKENS})")
    group.add_argument("--replicate", type=int, default=0,
                       help="draw an independent sample of every reply into its own run folder (default 0)")
    group = common.add_argument_group("cells")
    group.add_argument("--contexts", default="data/aacr/context_levels.jsonl", help="the context-level dataset file")
    group.add_argument("--dataset", default="data/aacr/positive_samples.json", help="AACR-Bench's file with the references")
    group.add_argument("--out-root", default="data/aacr/outcomes", help="where run folders go")
    group.add_argument("--run-name", help="run folder name (default <model>_<effort>_codex-rubric_v1)")
    group.add_argument("--entries", nargs="+", metavar="ID", help="only these PRs")
    group.add_argument("--first", type=int, metavar="N", help="only the first N PRs in dataset order (a pilot)")
    group.add_argument("--levels", nargs="+", metavar="LEVEL", help="only these levels (default all ten)")
    group.add_argument("--max-input-tokens", type=int, default=256_000,
                       help="skip cells above this many input tokens by the dataset's count (default 256000, "
                            "as for the Qwen reviewer)")
    group = common.add_argument_group("prices")
    group.add_argument("--price-in", type=float, help="USD per million input tokens, standard rate")
    group.add_argument("--price-out", type=float, help="USD per million output tokens, standard rate")
    group = common.add_argument_group("API calls")
    group.add_argument("--workers", type=int, help="parallel requests for count and direct mode (default 4; "
                                                   "1 with --via claude-code)")
    group.add_argument("--upload-grace", type=float, default=UPLOAD_GRACE, help=argparse.SUPPRESS)
    group = common.add_argument_group("plan limits (--via claude-code)")
    group.add_argument("--week-cap", type=float, default=90, metavar="PERCENT",
                       help="stop sending when this much of the plan's weekly limit is used, to leave some for "
                            "your own work (default 90; 100 runs to the limit)")
    group.add_argument("--five-hour-cap", type=float, metavar="PERCENT",
                       help="the same for the 5-hour limit (default: run to the limit)")
    group.add_argument("--wait-on-limit", action="store_true",
                       help="at a limit or cap, wait for it to reset and continue, instead of stopping")
    group.add_argument("--until", metavar="TIME",
                       help='send nothing after this local time: "HH:MM" or "YYYY-MM-DD HH:MM" (for example before '
                            "the weekly limit resets, so an unattended run does not start on the new week)")
    group.add_argument("--limit-wait-minutes", type=float, default=15, help=argparse.SUPPRESS)
    group.add_argument("--limit-margin-seconds", type=float, default=90, help=argparse.SUPPRESS)

    api = argparse.ArgumentParser(add_help=False)
    group = api.add_argument_group("sending")
    group.add_argument("--mode", default="batch", choices=("batch", "direct"),
                       help="--via api only. batch: Message Batches, half price, asynchronous (default); direct: "
                            "one call each, now")
    group.add_argument("--no-wait", action="store_true", help="batch mode: submit and exit; collect on a later run")
    group.add_argument("--batch-mb", type=float, default=64, help=f"largest batch upload in MB (default 64, limit {BATCH_MAX_MB})")
    group.add_argument("--poll-seconds", type=float, default=60, help="seconds between batch status checks (default 60)")
    group.add_argument("--yes", action="store_true", help="do not ask before sending paid requests")
    group.add_argument("--retry-rejected", action="store_true", help="send requests the API rejected as invalid again")

    judge = argparse.ArgumentParser(add_help=False)
    group = judge.add_argument_group("judge")
    group.add_argument("--judge", choices=("claude", "openai", "keyword-test"),
                       help="claude: through this API; openai: an OpenAI-compatible server; keyword-test: pipeline test only")
    group.add_argument("--judge-model", help=f"judge model id (claude: default {DEFAULT_MODEL})")
    group.add_argument("--judge-effort", default=DEFAULT_EFFORT, choices=EFFORTS, help=f"claude judge effort (default {DEFAULT_EFFORT})")
    group.add_argument("--judge-max-tokens", type=int, default=DEFAULT_JUDGE_MAX_TOKENS,
                       help=f"output limit per judge call (default {DEFAULT_JUDGE_MAX_TOKENS})")
    group.add_argument("--judge-base-url", help="openai judge: server URL, e.g. http://localhost:8001/v1")
    group.add_argument("--judge-api-key-env", default="JUDGE_API_KEY", help="openai judge: variable holding its key")

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", parents=[common], help="show what would be sent and a cost estimate (no network)")
    commands.add_parser("count", parents=[common], help="count the reviews' input tokens with the API (free)")
    commands.add_parser("review", parents=[common, api], help="get a review for every cell and write the review records")
    commands.add_parser("judge", parents=[common, api, judge], help="score the reviews and write the table")
    commands.add_parser("table", parents=[common], help="rebuild table.csv and coverage.csv from the saved records")
    commands.add_parser("run", parents=[common, api, judge], help="review, judge and table in one go")
    status = commands.add_parser("status", parents=[common], help="show batches and saved replies")
    status.add_argument("--account", action="store_true", help="also list the account's recent batches (needs the API)")
    collect = commands.add_parser("collect", parents=[common], help="save the results of finished batches")
    collect.add_argument("--adopt", metavar="BATCH_ID", help="also take over a batch that is not recorded locally")
    collect.add_argument("--again", action="store_true",
                         help="read the results of every recorded batch again (free; results are kept 29 days)")
    for name, text in (("code-login", "sign Claude Code in with your Claude plan, for --via claude-code"),
                       ("code-check", "two tiny calls through Claude Code, to confirm it works before a long run")):
        code = commands.add_parser(name, help=text)
        code.add_argument("--claude-bin", help="the Claude Code program (default: `claude` on PATH, else the desktop app's)")
        code.add_argument("--model", default=DEFAULT_MODEL, help=argparse.SUPPRESS)
        code.add_argument("--effort", default=DEFAULT_EFFORT, choices=EFFORTS, help=argparse.SUPPRESS)
    return parser


def main(argv=None, client_factory=make_client) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "batch_mb", 0) > BATCH_MAX_MB:
        raise SystemExit(f"--batch-mb cannot exceed the API's limit of {BATCH_MAX_MB} MB")
    if args.command in ("code-login", "code-check"):
        return (code_login if args.command == "code-login" else code_check)(args)
    run = Run(args)
    try:
        if args.command in ("review", "judge", "run", "collect"):
            with run.locked():
                return COMMANDS[args.command](run, client_factory)
        return COMMANDS[args.command](run, client_factory)
    except KeyboardInterrupt:
        run.log("stopped. Saved replies are kept and submitted batches keep running. Run the same command again "
                "to continue: nothing that was answered or submitted is sent again.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
