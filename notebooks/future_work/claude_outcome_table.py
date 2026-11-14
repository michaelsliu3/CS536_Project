"""Build the (PR x level) outcome table with Claude as the frozen reviewer, through the Claude API.

Future work, kept apart from the main pipeline. notebooks/outcome_table.ipynb reviews with
Qwen/Qwen3.8-27B on a cloud GPU, and the roadmap (Section 8.5.2) allows Claude only as a
fallback. This script writes a separate table in its own run folder, with the same prompt,
parser, AACR judge metric and file layout (src/review_outcomes/), so the two can be compared.
It needs no GPU: a laptop with data/aacr/ and an Anthropic API key is enough.

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

Every step resumes: a reply is saved once under <run folder>/api/replies/ and reused, so
re-running a command only sends what is still missing. See README.md in this folder.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.review_outcomes import backends, table  # noqa: E402
from src.review_outcomes.judge import score_review  # noqa: E402
from src.review_outcomes.parse import PARSER_VERSION  # noqa: E402
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
PROMPT_TOKENS = 1_710  # rubric, request sentence and chat template; the dataset's counts leave it out
SCORABLE = ("ok", "malformed", "empty_output")
STOP_TO_FINISH = {"end_turn": "stop", "stop_sequence": "stop", "max_tokens": "length"}
REQUEST_ID = re.compile(r"^[0-9a-f]{64}$")


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

    @property
    def identity(self) -> dict:
        """Goes into the outcome table's cache keys, like the other backends' `identity`."""
        return {"kind": "anthropic-messages", "model": self.model, "effort": self.effort,
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
        `custom_id`. A stored reply with this id is by construction the answer to this request."""
        payload = json.dumps({"params": self.params(messages), "replicate": self.replicate},
                             sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class ReplyStore:
    """Saved API replies, one JSON file per request id. Claude does not decode greedily, so a
    reply is saved once and reused by every later step and run; a cell is never re-sampled
    silently. Requests the API rejected as invalid are remembered too, and not sent again."""

    def __init__(self, root: Path):
        self.root = Path(root)

    def _path(self, kind: str, rid: str) -> Path:
        return self.root / kind / rid[:2] / f"{rid}.json"

    def has(self, rid: str) -> bool:
        return self._path("replies", rid).is_file()

    def get(self, rid: str) -> dict | None:
        return table.read_json(self._path("replies", rid))

    def put(self, rid: str, reply: dict) -> None:
        table.write_json(self._path("replies", rid), reply)
        self._path("rejected", rid).unlink(missing_ok=True)

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

    def __init__(self, spec: ClaudeSpec, store: ReplyStore):
        self.spec, self.store, self.identity = spec, store, spec.identity

    def __call__(self, messages: list[dict]) -> dict:
        rid = self.spec.request_id(messages)
        reply = self.store.get(rid)
        if reply is not None:
            return reply
        rejected = self.store.rejection(rid)
        if rejected:
            raise RuntimeError(f"the API rejected this request ({rejected['type']}): {rejected['message']}")
        raise PendingReply("no saved Claude reply for this request yet; run the review or judge step")


def reply_from_message(message, seconds: float | None = None, via: str = "direct", batch_id: str | None = None) -> dict:
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
        "via": via, "batch_id": batch_id, "received_at": now(),
    }


# --------------------------------------------------------------------------- calling the API

def make_client():
    try:
        import anthropic
    except ImportError:
        raise SystemExit("The Anthropic SDK is not installed: .venv/bin/python -m pip install anthropic") from None
    # Credentials come from the environment (ANTHROPIC_API_KEY, or an `ant auth login` profile).
    return anthropic.Anthropic(max_retries=5)


def fulfil_direct(client, pending: dict[str, Callable[[], dict]], store: ReplyStore, workers: int, log) -> dict:
    """Send each request now, at standard prices, `workers` at a time. Requests are streamed so
    long ones do not hit HTTP timeouts. A rejected request is remembered; one that fails after
    the SDK's retries stays pending. Authentication and permission errors stop the run."""
    import anthropic

    def one(rid: str, build: Callable[[], dict]) -> str:
        started = time.monotonic()
        try:
            with client.messages.stream(**build()) as stream:
                message = stream.get_final_message()
        except anthropic.BadRequestError as error:
            store.reject(rid, "invalid_request_error", str(error))
            return "rejected"
        except anthropic.RateLimitError as error:
            log(f"  request {rid[:12]} is still rate-limited after retries ({error}); it stays pending")
            return "failed"
        except anthropic.APIStatusError as error:
            if error.status_code < 500:
                raise  # wrong key, no access to the model, unknown model: no later request would work
            log(f"  request {rid[:12]} failed with HTTP {error.status_code} after retries; it stays pending")
            return "failed"
        except anthropic.APIConnectionError as error:
            log(f"  request {rid[:12]} failed ({type(error).__name__}) after retries; it stays pending")
            return "failed"
        store.put(rid, reply_from_message(message, round(time.monotonic() - started, 2)))
        return "saved"

    counts = {"saved": 0, "rejected": 0, "failed": 0}
    pool = ThreadPoolExecutor(max_workers=max(1, workers))
    try:
        futures = [pool.submit(one, rid, build) for rid, build in pending.items()]
        for done, future in enumerate(as_completed(futures), 1):
            counts[future.result()] += 1
            if done % 10 == 0 or done == len(futures):
                log(f"  {done}/{len(futures)} requests done: {counts}")
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    return counts


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

    A marker file is written before each upload and removed after it. Uploads are not retried
    automatically, because a retry after a lost response could create (and bill) a second batch;
    a marker left behind means the upload's outcome is unknown (see `status --account`)."""
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    uploader, created = client.with_options(timeout=1800, max_retries=0), []
    for number, group in enumerate(chunks(pending, int(max_mb * 1e6))):
        rids = [rid for rid, _ in group]
        marker = state_dir / f"submitting-{int(time.time() * 1000)}-{number}.json"
        table.write_json(marker, {"kind": kind, "requests": rids, "started_at": now()})
        batch = uploader.messages.batches.create(requests=[
            Request(custom_id=rid, params=MessageCreateParamsNonStreaming(**params)) for rid, params in group])
        table.write_json(state_dir / f"{batch.id}.json", {"id": batch.id, "kind": kind, "requests": rids,
                                                         "created_at": now(), "collected": False})
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


def _error_of(result) -> tuple[str, str]:
    """(type, message) of a batch result that is not a success."""
    error = getattr(result, "error", None)
    data = error.to_dict() if hasattr(error, "to_dict") else error if isinstance(error, dict) else {}
    inner = data.get("error") if isinstance(data.get("error"), dict) else data
    return str(inner.get("type") or result.type), str(inner.get("message") or "")


def collect_batches(client, state_dir: Path, store: ReplyStore, log) -> dict:
    """Save the results of every finished batch not collected yet. `open` counts the batches
    still processing. Requests that errored on the server, expired or were canceled are not
    billed and simply stay pending; invalid requests are remembered as rejected."""
    totals = {"open": 0, "saved": 0, "rejected": 0, "failed": 0}
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
        counts = {"saved": 0, "rejected": 0, "failed": 0}
        for item in client.messages.batches.results(state["id"]):  # any order: keyed by custom_id
            rid, result = item.custom_id, item.result
            if not REQUEST_ID.match(rid):
                continue  # an adopted batch that this script did not create
            if result.type == "succeeded":
                store.put(rid, reply_from_message(result.message, via="batch", batch_id=state["id"]))
                counts["saved"] += 1
            else:
                kind, message = _error_of(result)
                if kind.startswith("invalid_request"):
                    store.reject(rid, kind, message)
                    counts["rejected"] += 1
                else:
                    counts["failed"] += 1
        table.write_json(path, {**state, "collected": True, "collected_at": now(), "counts": counts})
        log(f"  batch {state['id']} ended: {counts}")
        for key, value in counts.items():
            totals[key] += value
    return totals


def wait_for_batches(client, state_dir: Path, store: ReplyStore, log, poll_seconds: float) -> None:
    while collect_batches(client, state_dir, store, log)["open"]:
        time.sleep(poll_seconds)


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
        self.spec = ClaudeSpec(args.model, args.effort, args.max_tokens, args.replicate)
        name = args.run_name or (f"{args.model}_{args.effort}_codex-rubric_v1"
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

    def write_reviews(self) -> list[dict]:
        """Turn the saved replies into the pipeline's review records (reviews/<entry>/<level>.json)."""
        return table.run_reviews(self.cells, ClaudeChat(self.spec, self.store), self.rubric, self.out,
                                 max_input_tokens=self.limit)

    def write_manifest(self, judge_identity=None) -> None:
        manifest = table.read_json(self.out / "manifest.json") or {}
        from src.tracer_context.aacr import file_sha256

        replies = [reply for rid in self.review_requests() if (reply := self.store.get(rid))]
        dates = sorted(reply["received_at"] for reply in replies)
        config = {key: value for key, value in vars(self.args).items() if key != "handler"}
        manifest.update({
            "profile": "claude-api (notebooks/future_work/claude_outcome_table.py)", "config": config,
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
        })
        if self.dataset.is_file():
            manifest.update(dataset=str(self.dataset), dataset_sha256=file_sha256(self.dataset))
        if judge_identity is not None:
            manifest["judge_identity"] = judge_identity
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

    def missing() -> list[str]:
        return [rid for rid in needed if not store.has(rid) and (args.retry_rejected or not store.rejection(rid))]

    run.batches.mkdir(parents=True, exist_ok=True)
    batches, markers = batch_states(run.batches)
    for marker in markers:
        run.log(f"WARNING: an earlier batch upload did not finish, so it is unknown whether a batch was created: "
                f"{marker}. Check `status --account`. If the batch exists, run `collect --adopt <batch id>`; "
                f"if not, delete that file. Its requests are held back until then.")
    if not missing():
        run.log(f"{kind}: all {len(needed)} requests already have a saved reply")
        return True
    client = client_factory()
    if any(not state.get("collected") for _, state in batches):
        collect_batches(client, run.batches, store, run.log)
    waiting = in_flight(run.batches)
    to_send = [rid for rid in missing() if rid not in waiting]
    run.log(f"{kind}: {len(needed)} requests, {len(needed) - len(missing())} answered, "
            f"{len(missing()) - len(to_send)} in a running batch, {len(to_send)} to send")
    if to_send:
        run.log(f"  {describe(to_send)}")
        how = ("as Message Batches (half price; most finish within an hour, at most 24 hours)" if args.mode == "batch"
               else f"directly, {args.workers} at a time, at standard prices")
        if not confirm(args, f"Send {len(to_send)} {kind} requests to {run_model(run, kind)} {how}?"):
            run.log("nothing sent")
            return False
        pending = {rid: needed[rid] for rid in to_send}
        if args.mode == "batch":
            submit_batches(client, pending, run.batches, kind, args.batch_mb, run.log)
        else:
            run.log(f"  direct: {fulfil_direct(client, pending, store, args.workers, run.log)}")
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


def run_model(run: Run, kind: str) -> str:
    return run.spec.model if kind == "review" else (run.args.judge_model or DEFAULT_MODEL)


# --------------------------------------------------------------------------- commands

def describe_selection(run: Run) -> None:
    requests = run.review_requests()
    too_long = sum(1 for cell in run.cells if run.limit is not None and (cell.input_tokens or 0) > run.limit)
    run.log(f"run folder: {run.out}")
    run.log(f"reviewer: {run.spec.model}, effort {run.spec.effort}, max_tokens {run.spec.max_tokens:,}"
            + (f", replicate {run.spec.replicate}" if run.spec.replicate else ""))
    run.log(f"cells: {len(run.cells)} from {len(run.entries)} PRs; {too_long} over {run.limit:,} input tokens "
            f"(not sent); {len(run.cells) - too_long - len(requests)} identical to another level (reviewed once); "
            f"{len(requests)} requests")


def cmd_plan(run: Run, client_factory) -> int:
    describe_selection(run)
    requests, store = run.review_requests(), run.store
    answered = [rid for rid in requests if store.has(rid)]
    rejected = [rid for rid in requests if not store.has(rid) and store.rejection(rid)]
    waiting = in_flight(run.batches) & set(requests)
    to_send = [rid for rid in requests if rid not in set(answered) | set(rejected) | waiting]
    run.log(f"requests: {len(answered)} answered, {len(rejected)} rejected by the API, {len(waiting)} in a running "
            f"batch, {len(to_send)} to send")
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
    if run.mean_output_tokens(requests) is None:
        run.log("output (thinking plus the review) is billed on top; a pilot shows how much it is per review")
    return 0


def cmd_count(run: Run, client_factory) -> int:
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
                + ("" if mean_out else "; output (thinking plus the review) is billed on top"))

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
        prices = run.prices(run.spec.model)
        if prices:
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
    spec = ClaudeSpec(args.judge_model or DEFAULT_MODEL, args.judge_effort, args.judge_max_tokens)
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
                    + run.cost_text(spec.model, tokens, None, False) + "; output is billed on top")

        needed = {rid: (lambda prompt=prompt: spec.params(user_message(prompt))) for rid, prompt in prompts.items()}
        if not fulfil(run, needed, "judge", describe, client_factory):
            return 2
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
                f"{state.get('created_at')}): " + (f"collected {state.get('counts')}" if state.get("collected") else "not collected yet"))
    for marker in markers:
        run.log(f"upload with unknown outcome: {marker}")
    requests = run.review_requests()
    run.log(f"review replies saved: {sum(run.store.has(rid) for rid in requests)} of {len(requests)}")
    if run.args.account:
        run.log("the account's most recent batches:")
        for number, batch in enumerate(client_factory().messages.batches.list(limit=20)):
            if number == 20:
                break
            run.log(f"  {batch.id} {batch.processing_status} created {batch.created_at} {batch.request_counts}")
    return 0


def cmd_collect(run: Run, client_factory) -> int:
    run.batches.mkdir(parents=True, exist_ok=True)
    if run.args.adopt:  # a batch whose upload response was lost: its custom_ids are request ids
        table.write_json(run.batches / f"{run.args.adopt}.json", {"id": run.args.adopt, "kind": "adopted",
                                                                 "requests": None, "created_at": None, "collected": False})
        for marker in batch_states(run.batches)[1]:
            marker.unlink()
    totals = collect_batches(client_factory(), run.batches, run.store, run.log)
    run.log(f"collected: {totals}")
    return 0


COMMANDS = {"plan": cmd_plan, "count": cmd_count, "review": cmd_review, "judge": cmd_judge, "table": cmd_table,
            "run": cmd_run, "status": cmd_status, "collect": cmd_collect}


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    group = common.add_argument_group("reviewer")
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
    group.add_argument("--workers", type=int, default=4, help="parallel requests for count and direct mode (default 4)")

    api = argparse.ArgumentParser(add_help=False)
    group = api.add_argument_group("sending")
    group.add_argument("--mode", default="batch", choices=("batch", "direct"),
                       help="batch: Message Batches, half price, asynchronous (default); direct: one call each, now")
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
    return parser


def main(argv=None, client_factory=make_client) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "batch_mb", 0) > BATCH_MAX_MB:
        raise SystemExit(f"--batch-mb cannot exceed the API's limit of {BATCH_MAX_MB} MB")
    run = Run(args)
    try:
        return COMMANDS[args.command](run, client_factory)
    except KeyboardInterrupt:
        run.log("stopped. Saved replies are kept and submitted batches keep running; run the command again to resume.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
