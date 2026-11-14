"""Tests for notebooks/future_work/claude_outcome_table.py with a stand-in API client: no network,
no cost. The tests that use the SDK's request types are skipped when `anthropic` is not installed.

Most of them are about money: a run can be stopped anywhere and continued, and a request that
was answered or submitted is never sent again."""
import contextlib
import importlib.util
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from src.review_outcomes import table
from src.review_outcomes.prompts import load_rubric, review_messages
from src.tracer_context.dataset import append_record

SCRIPT = Path(__file__).resolve().parents[1] / "notebooks" / "future_work" / "claude_outcome_table.py"
spec = importlib.util.spec_from_file_location("claude_outcome_table", SCRIPT)
cot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cot
spec.loader.exec_module(cot)

HAVE_SDK = importlib.util.find_spec("anthropic") is not None
NO_CREDIT = "Your credit balance is too low to access the Anthropic API. Please go to Plans & Billing to purchase credits."
REFS = [
    {"note": "unhashable value passed to cached warning_once", "path": "p.py", "from_line": 249, "to_line": 249},
    {"note": "use %d for token count", "path": "p.py", "from_line": 218, "to_line": 224, "is_ai_comment": True},
]
REVIEW = json.dumps({"findings": [{"title": "Dict passed to cached warning_once", "body": "raises TypeError",
                                   "confidence_score": 0.8, "priority": 1,
                                   "code_location": {"absolute_file_path": "/repo/p.py",
                                                     "line_range": {"start": 249, "end": 249}}}],
                     "overall_correctness": "patch is incorrect"})
# The same kind of review as the model sometimes writes it: a Markdown list, not the rubric's JSON.
MARKDOWN_REVIEW = """**Findings**

1. **[P1] Dict passed to cached `warning_once`** (`p.py`, lines 249–249; confidence 0.8)

   The dict is unhashable, so the cached call raises `TypeError`.

2. **[P3] Use `%d` for the token count** — `p.py`, line 218
   Confidence: 0.4

   - The count is an integer.

**Overall verdict: patch is incorrect** (confidence 0.7)

The warning path fails at run time.
"""


def message(text, stop_reason="end_turn", input_tokens=120, output_tokens=40, id="msg_test", **extra):
    """A stand-in for the SDK's Message: a thinking block (empty by default on Opus) and a text block."""
    content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, stop_reason=stop_reason, model="claude-opus-5-5", id=id,
                           usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens), **extra)


# A stand-in for the Claude Code program. It records how it was called and prints a result in the
# shape Claude Code 2.1 prints with --output-format json. Pointed at another server (as the script
# does to inspect a request), it posts the request it would send there, with the same kinds of
# additions the real program makes.
FAKE_CLAUDE = r"""#!@PYTHON@
import json, os, sys, urllib.error, urllib.request
from pathlib import Path

state, args = Path(os.environ["FAKE_CLAUDE_STATE"]), sys.argv[1:]
if args[:2] == ["auth", "status"]:
    signed_in = not (state / "signed_out").exists()
    print(json.dumps({"loggedIn": signed_in, "authMethod": "claude.ai" if signed_in else "none",
                      "subscriptionType": "max" if signed_in else None}))
    sys.exit(0)
if args == ["--version"]:
    print("9.9.9 (Claude Code)")
    sys.exit(0)
prompt, system = sys.stdin.read(), args[args.index("--system-prompt") + 1]
if os.environ.get("ANTHROPIC_BASE_URL"):
    body = {
        "model": args[args.index("--model") + 1], "max_tokens": 1000,
        "tools": [{"name": "Bash"}] if (state / "tools_leak").exists() else [],
        "system": [{"type": "text", "text": "x-anthropic-billing-header: cc_version=9.9.9.1;"},
                   {"type": "text", "text": "You are a Claude agent, built on Anthropic's Claude Agent SDK."}]
                  + ([{"type": "text", "text": system}] if system else []),
        "messages": [{"role": "user", "content": [
                         {"type": "text", "text": "<system-reminder>The user's email address is someone@example.com</system-reminder>"},
                         {"type": "text", "text": prompt}]},
                     {"role": "system", "content": "# Environment\n - Primary working directory: " + os.getcwd()
                                                   + "\nToday's date is 2026-11-13."}]}
    request = urllib.request.Request(os.environ["ANTHROPIC_BASE_URL"] + "/v1/messages?beta=true",
                                     data=json.dumps(body).encode(), headers={"content-type": "application/json"})
    try:
        urllib.request.urlopen(request, timeout=30)
    except urllib.error.HTTPError:
        pass
    print(json.dumps({"type": "result", "is_error": True, "result": "API Error: 400 inspection only"}))
    sys.exit(1)
calls = state / "calls.jsonl"
number = len(calls.read_text().splitlines()) + 1 if calls.exists() else 1
with calls.open("a") as handle:
    handle.write(json.dumps({
        "args": args, "prompt": prompt, "cwd": os.getcwd(), "api_key": "ANTHROPIC_API_KEY" in os.environ,
        "max_output": os.environ.get("CLAUDE_CODE_MAX_OUTPUT_TOKENS"),
        "no_cache": os.environ.get("DISABLE_PROMPT_CACHING"),
        "claude_vars": sorted(k for k in os.environ if k.startswith("CLAUDE") and k != "CLAUDE_CODE_MAX_OUTPUT_TOKENS"),
    }) + "\n")


def result(text, error=False, status=None):
    # --output-format stream-json: an init line, the plan-limit report, then the result.
    import time
    settings = json.loads((state / "limits.json").read_text()) if (state / "limits.json").exists() else {}
    reset = int(time.time()) + settings.get("reset_in", 3600)
    windows = {name: {"utilization": round(start + step * number, 4), "resetsAt": reset, "status": "allowed"}
               for name, (start, step) in {"five_hour": settings.get("five_hour", [0.10, 0]),
                                           "seven_day": settings.get("seven_day", [0.05, 0])}.items()}
    print(json.dumps({"type": "system", "subtype": "init", "tools": [], "model": "claude-opus-5-5"}))
    print(json.dumps({"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected" if status == 429 else "allowed", "resetsAt": reset, "rateLimitType": "five_hour",
        "overageStatus": settings.get("overage_status", "rejected"), "isUsingOverage": settings.get("overage", False),
        "unifiedWindows": windows}}))
    print(json.dumps({"type": "result", "subtype": "success", "is_error": error, "result": text, "num_turns": 1,
                      "stop_reason": "end_turn", "api_error_status": status, "total_cost_usd": 0.02,
                      "uuid": f"uuid-{number}", "session_id": "s",
                      "usage": {"input_tokens": 30, "cache_creation_input_tokens": 70, "cache_read_input_tokens": 0,
                                "output_tokens": 40},
                      "modelUsage": {} if error else {"claude-opus-5-5": {"outputTokens": 40}}}))
    sys.exit(1 if error else 0)


limit, once = state / "limit_at", state / "limit_once"
if (limit.exists() and number >= int(limit.read_text())) or (once.exists() and number == int(once.read_text())):
    result("You've hit your session limit, resets 3pm", error=True, status=429)
if system.isspace():
    result("API Error: 400 system: text content blocks must contain non-whitespace text", error=True, status=400)
result(@REVIEW@ if system else "1. Match\n[1]")
"""


def request_id(params):
    """The script's id for a request the stand-in client received."""
    messages = ([{"role": "system", "content": params["system"]}] if "system" in params else []) + params["messages"]
    return cot.ClaudeSpec(params["model"], params["output_config"]["effort"]).request_id(messages)


class FakeClient:
    """Answers reviews with REVIEW and judge prompts with "[1]". Batches finish on the second check.

    `sent` holds every request that reached the model (each one would be billed), `answered` the
    ids of those that got a reply."""

    def __init__(self):
        batches = SimpleNamespace(create=self._create, retrieve=self._retrieve, results=self._results, list=self._list)
        self.messages = SimpleNamespace(stream=self._stream, count_tokens=self._count, batches=batches)
        self.sent, self.answered, self.batches, self.created, self.checks = [], [], {}, {}, {}
        self.options, self.invalid = [], set()
        self.interrupt_at = None  # the Nth request raises KeyboardInterrupt instead of being answered
        self.upload_fails = None  # "before": no batch is created; "after": it is, but the response is lost
        self.cut_off = None  # (text in the request, limit): cut the reply off while max_tokens is below the limit
        self.no_credit_at = None  # from the Nth direct request on, the API says the credit balance is too low
        self.no_credit = set()  # batch requests (custom_id) that were not processed for lack of credit
        self.markdown = None  # text in the request: that review is written as a Markdown list, not JSON

    def with_options(self, **options):
        self.options.append(options)
        return self

    def answer(self, params):
        self.sent.append(params)
        if self.interrupt_at == len(self.sent):
            raise KeyboardInterrupt
        if self.no_credit_at is not None and len(self.sent) >= self.no_credit_at:
            import anthropic
            import httpx2
            response = httpx2.Response(400, request=httpx2.Request("POST", "https://api.anthropic.com/v1/messages"))
            raise anthropic.BadRequestError(NO_CREDIT, response=response, body=None)
        self.answered.append(request_id(params))
        number = f"msg_{len(self.sent):03d}"
        if self.cut_off and self.cut_off[0] in params["messages"][0]["content"] and params["max_tokens"] < self.cut_off[1]:
            return message('{"findings": [', stop_reason="max_tokens", id=number)
        if self.markdown and "system" in params and self.markdown in params["messages"][0]["content"]:
            return message(MARKDOWN_REVIEW, id=number)
        return message(REVIEW if "system" in params else "1. Match\n[1]", id=number)

    def _stream(self, **params):
        client = self

        class Stream:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def get_final_message(self):
                return client.answer(params)
        return Stream()

    def _count(self, **params):
        return SimpleNamespace(input_tokens=len(params["messages"][0]["content"]) + len(params.get("system", "")))

    def _create(self, requests):
        failure, self.upload_fails = self.upload_fails, None
        if failure == "before":
            raise ConnectionError("the upload did not go through")
        batch_id = f"msgbatch_{len(self.batches) + 1:02d}"
        self.batches[batch_id], self.created[batch_id] = list(requests), datetime.now(timezone.utc)
        if failure == "after":
            raise ConnectionError("the batch was created, but the response was lost")
        return SimpleNamespace(id=batch_id, processing_status="in_progress")

    def _batch(self, batch_id, ended):
        counts = SimpleNamespace(processing=0 if ended else len(self.batches[batch_id]), succeeded=0, errored=0)
        return SimpleNamespace(id=batch_id, processing_status="ended" if ended else "in_progress",
                               request_counts=counts, created_at=self.created[batch_id], results_url="https://results")

    def _retrieve(self, batch_id):
        self.checks[batch_id] = self.checks.get(batch_id, 0) + 1
        return self._batch(batch_id, self.checks[batch_id] >= 2)

    def _list(self, limit=20):
        return [self._batch(batch_id, self.checks.get(batch_id, 0) >= 2) for batch_id in reversed(self.batches)]

    def _results(self, batch_id):
        for request in reversed(self.batches[batch_id]):  # results come back in any order
            if request["custom_id"] in self.invalid | self.no_credit:
                text = "prompt is too long" if request["custom_id"] in self.invalid else NO_CREDIT
                error = {"type": "error", "error": {"type": "invalid_request_error", "message": text}}
                yield SimpleNamespace(custom_id=request["custom_id"], result=SimpleNamespace(type="errored", error=error))
            else:
                yield SimpleNamespace(custom_id=request["custom_id"],
                                      result=SimpleNamespace(type="succeeded", message=message(REVIEW, id=f"msg_{request['custom_id'][:8]}")))


class RequestTests(unittest.TestCase):
    def test_request_shape_and_id(self):
        spec = cot.ClaudeSpec()
        messages = review_messages("the context", "RUBRIC")
        params = spec.params(messages)
        self.assertEqual(params["model"], "claude-opus-5-5")
        self.assertEqual(params["output_config"], {"effort": "medium"})
        self.assertEqual(params["system"], "RUBRIC")
        self.assertEqual([m["role"] for m in params["messages"]], ["user"])
        # Rejected by the model or unwanted here: sampling settings, a thinking override, a fallback model.
        self.assertFalse({"temperature", "top_p", "top_k", "thinking", "fallbacks"} & set(params))
        self.assertNotIn("system", spec.params(cot.user_message("judge prompt")))

        rid = spec.request_id(messages)
        self.assertRegex(rid, cot.REQUEST_ID)  # also a valid batch custom_id: 64 characters of [0-9a-f]
        self.assertEqual(rid, cot.ClaudeSpec().request_id(review_messages("the context", "RUBRIC")))
        others = [cot.ClaudeSpec(effort="high"), cot.ClaudeSpec(model="claude-sonnet-5-5"), cot.ClaudeSpec(replicate=1)]
        self.assertEqual(len({rid, *(other.request_id(messages) for other in others),
                              spec.request_id(review_messages("another context", "RUBRIC"))}), 5)
        # The output limit is not part of the id: raising it must not make every reply look unanswered.
        self.assertEqual(rid, cot.ClaudeSpec(max_tokens=1000).request_id(messages))
        self.assertEqual(spec.identity["effort"], "medium")
        # The API and Claude Code's print mode are different reviewers: they never share a reply.
        through_code = cot.ClaudeSpec(channel=cot.VIA_CODE)
        self.assertNotEqual(rid, through_code.request_id(messages))
        self.assertEqual((spec.identity["kind"], through_code.identity["kind"]), ("anthropic-messages", "claude-code-print"))

    def test_reply_from_message(self):
        reply = cot.reply_from_message(message("the review"), seconds=1.5, max_tokens=500)
        self.assertEqual((reply["text"], reply["finish_reason"], reply["prompt_tokens"], reply["completion_tokens"]),
                         ("the review", "stop", 120, 40))
        self.assertEqual((reply["served_model"], reply["via"], reply["refusal"], reply["max_tokens"]),
                         ("claude-opus-5-5", "direct", None, 500))
        self.assertEqual(cot.reply_from_message(message("{", stop_reason="max_tokens"))["finish_reason"], "length")
        details = SimpleNamespace(category="cyber", explanation="declined")
        refused = cot.reply_from_message(message("", stop_reason="refusal", stop_details=details), via="batch", batch_id="b1")
        self.assertEqual(refused["refusal"], {"category": "cyber", "explanation": "declined"})
        self.assertEqual((refused["text"], refused["stop_reason"], refused["batch_id"]), ("", "refusal", "b1"))

    def test_chunks_respect_the_batch_limits(self):
        pending = {f"{i:064x}": (lambda i=i: {"messages": [{"role": "user", "content": "x" * 1000}]}) for i in range(5)}
        self.assertEqual([len(group) for group in cot.chunks(pending, max_bytes=2600)], [2, 2, 1])
        self.assertEqual([len(group) for group in cot.chunks(pending, max_bytes=10**9, max_requests=4)], [4, 1])
        self.assertEqual([len(group) for group in cot.chunks(pending, max_bytes=10)], [1] * 5)  # an oversized request goes alone

    def test_the_first_reply_to_a_request_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            store, one, two = cot.ReplyStore(Path(tmp)), "a" * 64, "b" * 64
            self.assertFalse(store.answered(one, 100))
            self.assertTrue(store.put(one, {"text": "first", "finish_reason": "stop", "max_tokens": 100}))
            self.assertFalse(store.put(one, {"text": "second", "finish_reason": "stop", "max_tokens": 900}))
            self.assertEqual(store.get(one)["text"], "first")
            self.assertTrue(store.answered(one, 10**6))  # a complete reply settles the request at any limit

            cut = {"text": "{", "finish_reason": "length", "max_tokens": 100}
            store.put(two, cut)
            self.assertTrue(store.answered(two, 100))    # same limit: asking again would cut it off again
            self.assertFalse(store.answered(two, 200))   # a higher limit: worth asking again
            self.assertFalse(store.put(two, {**cut, "text": "again"}))
            self.assertTrue(store.put(two, {"text": "whole", "finish_reason": "stop", "max_tokens": 200}))
            self.assertEqual(store.get(two)["text"], "whole")


class MarkdownReviewTests(unittest.TestCase):
    """A review written as a Markdown list is read into the rubric's JSON shape; anything else is left alone."""

    def test_reads_a_review_written_as_a_list(self):
        review = cot.markdown_review(MARKDOWN_REVIEW, ["p.py"])
        first, second = review["findings"]
        self.assertEqual(first, {"title": "[P1] Dict passed to cached `warning_once`", "priority": 1, "confidence_score": 0.8,
                                 "body": "The dict is unhashable, so the cached call raises `TypeError`.",
                                 "code_location": {"absolute_file_path": "p.py", "line_range": {"start": 249, "end": 249}}})
        self.assertEqual((second["title"], second["priority"], second["confidence_score"], second["body"]),
                         ("[P3] Use `%d` for the token count", 3, 0.4, "- The count is an integer."))
        self.assertEqual(second["code_location"]["line_range"], {"start": 218, "end": 218})
        self.assertEqual((review["overall_correctness"], review["overall_confidence_score"], review["overall_explanation"]),
                         ("patch is incorrect", 0.7, "The warning path fails at run time."))

    def test_reads_the_other_layouts_and_completes_a_bare_file_name(self):
        text = ("**Findings**\n\n"
                "- **[P2] Close the handle.** Confidence 0.6. File: `src/io/reader.c`, lines 10-12.\n\n"
                "  The handle leaks on the error path.\n\n"
                "- **[P1] Check the index**\n"
                "  - **Location:** `src/io/index.c`, lines 40–44\n"
                "  - **Confidence:** 0.75\n"
                "  - The loop reads one element past the end.\n"
                "  - Fix: stop at `n - 1`.\n\n"
                "- **[P3] Rename the flag**\n"
                "  - Location: `reader.c:7-8` (`open_reader`)\n"
                "  - It shadows a global.\n\n"
                "**Overall correctness:** patch is incorrect.\n")
        review = cot.markdown_review(text, ["src/io/reader.c", "src/io/index.c", "src/io/reader.h"])
        found = [(f["title"], f.get("confidence_score"), f["code_location"]["absolute_file_path"],
                  f["code_location"]["line_range"], f["body"]) for f in review["findings"]]
        self.assertEqual(found, [
            ("[P2] Close the handle.", 0.6, "src/io/reader.c", {"start": 10, "end": 12}, "The handle leaks on the error path."),
            ("[P1] Check the index", 0.75, "src/io/index.c", {"start": 40, "end": 44},
             "- The loop reads one element past the end.\n- Fix: stop at `n - 1`."),
            ("[P3] Rename the flag", None, "src/io/reader.c", {"start": 7, "end": 8}, "- It shadows a global."),
        ])
        self.assertEqual(review["findings"][2]["code_location"]["path_as_written"], "reader.c")
        self.assertEqual(review["overall_correctness"], "patch is incorrect")
        self.assertNotIn("overall_confidence_score", review)
        # A name that fits two of the PR's files is left as the model wrote it.
        again = cot.markdown_review(text, ["src/io/reader.c", "lib/reader.c"])
        self.assertEqual(again["findings"][2]["code_location"]["absolute_file_path"], "reader.c")

    def test_a_review_that_says_it_found_nothing(self):
        review = cot.markdown_review("**Findings**\n\nNo issues found.\n\n**Overall verdict: patch is correct** (confidence 0.9)")
        self.assertEqual((review["findings"], review["overall_correctness"]), ([], "patch is correct"))

    def test_text_it_does_not_recognise_is_left_alone(self):
        for text in ("", "The patch looks fine to me.",
                     # findings without the priority tag: an unknown layout must not be read as "no findings"
                     "**Findings**\n\n1. **No null check** (`a.c`, line 3)\n\n**Overall verdict: patch is incorrect**",
                     "**Findings**\n\n1. **No null check in the parser**\n\n   It can crash.\n\n**Overall: patch is incorrect**"):
            self.assertIsNone(cot.markdown_review(text, ["a.c"]), text)
        for text in (REVIEW, "Here it is:\n```json\n" + REVIEW + "\n```", "no review here"):
            reply = {"text": text}
            self.assertIs(cot.as_json_review(reply, ["p.py"]), reply)  # JSON, or unreadable: unchanged
        converted = cot.as_json_review({"text": MARKDOWN_REVIEW, "seconds": 3}, ["p.py"])
        self.assertEqual((converted["original_text"], converted["seconds"], converted["converted"]["by"]),
                         (MARKDOWN_REVIEW, 3, cot.MARKDOWN_VERSION))
        self.assertEqual(len(json.loads(converted["text"])["findings"]), 2)


class RunTests(unittest.TestCase):
    """The whole flow on a two-PR dataset in a temporary folder."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        levels = {"L01_min": {"text": "context one", "input_tokens": 100},
                  "L02_function": {"text": "context one", "input_tokens": 100},  # identical to its parent
                  "L03_file": {"text": "context three", "input_tokens": 900},
                  "L10_all": {"text": "a very long context", "input_tokens": 300_000}}
        append_record(self.root / "contexts.jsonl", {"id": "o__r__1", "index": 0, "status": "ok", "levels": levels,
                                                     "known_paths": ["p.py"]})
        append_record(self.root / "contexts.jsonl", {"id": "o__r__2", "index": 1, "status": "ok", "known_paths": [],
                                                     "levels": {"L01_min": {"text": "second pr", "input_tokens": 50}}})
        records = [{"githubPrUrl": f"https://github.com/o/r/pull/{n}", "project_main_language": "Python", "comments": REFS}
                   for n in (1, 2)]
        (self.root / "refs.json").write_text(json.dumps(records), encoding="utf-8")
        self.client = FakeClient()
        self.base = ["--contexts", str(self.root / "contexts.jsonl"), "--dataset", str(self.root / "refs.json"),
                     "--out-root", str(self.root / "out"), "--upload-grace", "0"]
        self.out = self.root / "out" / "claude-opus-5-5_medium_codex-rubric_v1"

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *argv, client_factory=None):
        with contextlib.redirect_stdout(io.StringIO()):  # the script's progress lines
            return cot.main([argv[0], *self.base, *argv[1:]], client_factory=client_factory or (lambda: self.client))

    def review(self, level):
        return table.read_json(self.out / "reviews" / "o__r__1" / f"{level}.json")

    def statuses(self):
        return sorted(table.read_json(path)["status"] for path in (self.out / "reviews").rglob("*.json"))

    def test_plan_needs_no_client_and_writes_nothing(self):
        def no_client():
            raise AssertionError("plan must not call the API")
        self.assertEqual(self.run_cli("plan", client_factory=no_client), 0)
        self.assertFalse((self.root / "out").exists())

    def test_count_saves_token_counts(self):
        self.assertEqual(self.run_cli("count"), 0)
        run = cot.Run(cot.build_parser().parse_args(["plan", *self.base]))
        requests = run.review_requests()
        self.assertEqual(len(requests), 3)  # the identical level and the too-long level add no request
        tokens, counted = run.review_tokens(requests)
        self.assertEqual(counted, 3)
        rubric = load_rubric()
        self.assertEqual(tokens, sum(len(review_messages(cell.text, rubric)[1]["content"]) + len(rubric)
                                     for cell in requests.values()))

    @unittest.skipUnless(HAVE_SDK, "needs the anthropic package")
    def test_direct_review_judge_and_table(self):
        self.assertEqual(self.run_cli("review", "--mode", "direct", "--yes"), 0)
        self.assertEqual(len(self.client.sent), 3)
        self.assertTrue(all(params["output_config"] == {"effort": "medium"} for params in self.client.sent))
        review = self.review("L01_min")
        self.assertEqual((review["status"], review["backend"]["model"], review["reply"]["served_model"]),
                         ("ok", "claude-opus-5-5", "claude-opus-5-5"))
        self.assertEqual(review["parsed"]["findings"][0]["path"], "p.py")
        self.assertEqual(self.review("L02_function")["reused_from"], "L01_min")
        self.assertEqual(self.review("L10_all")["status"], "too_long")

        def no_client():
            raise AssertionError("everything is answered: the API must not be touched")
        self.assertEqual(self.run_cli("review", "--mode", "direct", "--yes", client_factory=no_client), 0)
        self.assertEqual(len(self.client.sent), 3)  # the saved replies were reused: no cell is sampled twice

        self.assertEqual(self.run_cli("judge", "--judge", "claude", "--mode", "direct", "--yes"), 0)
        # AACR's judge is asked once per finding on a file that has references. The three reviews here
        # hold the same finding, so their judge prompts are identical and are answered by one call.
        self.assertEqual(len(self.client.sent), 4)
        rows = json.loads((self.out / "table.json").read_text(encoding="utf-8"))
        self.assertEqual(len(rows), 5)
        first = next(row for row in rows if (row["entry"], row["level"]) == ("o__r__1", "L01_min"))
        self.assertEqual((first["covered"], first["expected"], first["review_status"]), (1, 2, "ok"))
        manifest = table.read_json(self.out / "manifest.json")
        self.assertEqual(manifest["reviewer_identity"]["model"], "claude-opus-5-5")
        self.assertEqual(manifest["reviewer_api"]["served_models"], ["claude-opus-5-5"])
        self.assertEqual(manifest["judge_identity"]["kind"], "anthropic-messages")

        self.assertEqual(self.run_cli("judge", "--judge", "claude", "--mode", "direct", "--yes", client_factory=no_client), 0)
        self.assertEqual(self.run_cli("table"), 0)  # rebuilds from the saved records, no API call
        self.assertEqual(len(self.client.sent), 4)

    @unittest.skipUnless(HAVE_SDK, "needs the anthropic package")
    def test_a_review_written_as_markdown_is_read_and_not_asked_for_again(self):
        self.client.markdown = "context three"
        self.assertEqual(self.run_cli("review", "--mode", "direct", "--yes"), 0)
        self.assertEqual(self.statuses(), ["ok", "ok", "ok", "ok", "too_long"])  # none is malformed
        review = self.review("L03_file")
        self.assertEqual([(f["path"], f["from_line"], f["to_line"], f["priority"], f["confidence"])
                          for f in review["parsed"]["findings"]], [("p.py", 249, 249, 1, 0.8), ("p.py", 218, 218, 3, 0.4)])
        self.assertEqual((review["reply"]["original_text"], review["reply"]["converted"]["by"], review["backend"]["markdown_replies"]),
                         (MARKDOWN_REVIEW, cot.MARKDOWN_VERSION, cot.MARKDOWN_VERSION))
        self.assertNotIn("converted", self.review("L01_min")["reply"])  # a JSON reply is passed on as it came
        # The saved reply is the model's own text: the conversion happens when the records are written.
        run = cot.Run(cot.build_parser().parse_args(["plan", *self.base]))
        saved = [run.store.get(rid) for rid, cell in run.review_requests().items() if cell.level == "L03_file"]
        self.assertEqual([(reply["text"], "converted" in reply) for reply in saved], [(MARKDOWN_REVIEW, False)])
        manifest = table.read_json(self.out / "manifest.json")
        self.assertEqual(manifest["markdown_replies"], {"converter": cot.MARKDOWN_VERSION, "not_json": 1, "read_as_markdown": 1})

        self.assertEqual(self.run_cli("judge", "--judge", "claude", "--mode", "direct", "--yes"), 0)
        self.assertEqual(len(self.client.sent), 3 + 3)  # one judge call for the JSON finding, two for the converted ones
        rows = {(row["entry"], row["level"]): row for row in json.loads((self.out / "table.json").read_text(encoding="utf-8"))}
        self.assertEqual((rows["o__r__1", "L03_file"]["findings"], rows["o__r__1", "L03_file"]["generated"]), (2, 2))

        def no_client():
            raise AssertionError("everything is answered: the API must not be touched")
        self.assertEqual(self.run_cli("review", "--mode", "direct", "--yes", client_factory=no_client), 0)
        self.assertEqual(self.run_cli("table"), 0)

    @unittest.skipUnless(HAVE_SDK, "needs the anthropic package")
    def test_stopping_a_direct_run_never_repeats_an_answered_request(self):
        self.client.interrupt_at = 2  # the run is stopped while the second request is out
        self.assertEqual(self.run_cli("review", "--mode", "direct", "--yes", "--workers", "1"), 130)
        self.assertEqual(len(self.client.answered), 1)
        self.assertEqual(len(self.client.sent), 2)  # nothing more was started once the run was stopping
        saved = list((self.out / "api" / "replies").rglob("*.json"))
        self.assertEqual(len(saved), 1)  # the answer that arrived was saved before the run ended

        self.assertEqual(self.run_cli("review", "--mode", "direct", "--yes", "--workers", "1"), 0)
        self.assertEqual(len(self.client.answered), 3)
        self.assertEqual(len(set(self.client.answered)), 3)  # each request was answered exactly once overall
        self.assertEqual(self.statuses(), ["ok", "ok", "ok", "ok", "too_long"])

    @unittest.skipUnless(HAVE_SDK, "needs the anthropic package")
    def test_batch_review_resumes_and_remembers_rejections(self):
        self.assertEqual(self.run_cli("review", "--yes", "--no-wait"), 2)  # submitted, not finished
        self.assertEqual(len(self.client.batches), 1)
        self.assertEqual(self.client.options[-1], {"timeout": 1800, "max_retries": 0})  # uploads are never retried
        requests = self.client.batches["msgbatch_01"]
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(cot.REQUEST_ID.match(request["custom_id"]) for request in requests))
        self.assertEqual(self.review("L01_min")["status"], "error")  # pending

        self.assertEqual(self.run_cli("review", "--yes", "--no-wait"), 2)  # stopped and started again while it runs
        self.assertEqual(len(self.client.batches), 1)  # the submitted requests are not submitted again

        self.client.invalid.add(requests[0]["custom_id"])
        self.assertEqual(self.run_cli("review", "--yes", "--poll-seconds", "0"), 0)  # collects; nothing new is sent
        self.assertEqual(len(self.client.batches), 1)
        state = table.read_json(self.out / "api" / "batches" / "msgbatch_01.json")
        self.assertEqual((state["collected"], state["counts"]),
                         (True, {"saved": 2, "already_saved": 0, "rejected": 1, "failed": 0}))
        self.assertEqual(self.statuses().count("too_long"), 1)
        rejected = next(table.read_json(path) for path in (self.out / "reviews").rglob("*.json")
                        if "rejected this request" in (table.read_json(path).get("error") or ""))
        self.assertIn("prompt is too long", rejected["error"])

        self.assertEqual(self.run_cli("review", "--yes", "--poll-seconds", "0"), 0)  # the rejection is not resent
        self.assertEqual(len(self.client.batches), 1)

    @unittest.skipUnless(HAVE_SDK, "needs the anthropic package")
    def test_a_lost_upload_response_does_not_lead_to_a_second_batch(self):
        self.client.upload_fails = "after"  # the batch exists (and will be billed), but this run never learned its id
        with self.assertRaises(ConnectionError):
            self.run_cli("review", "--yes", "--poll-seconds", "0")
        self.assertEqual(len(self.client.batches), 1)
        self.assertEqual(len(list((self.out / "api" / "batches").glob("submitting-*.json"))), 1)

        self.assertEqual(self.run_cli("review", "--yes", "--poll-seconds", "0"), 0)
        self.assertEqual(len(self.client.batches), 1)  # found on the account and taken over, not sent again
        state = table.read_json(self.out / "api" / "batches" / "msgbatch_01.json")
        self.assertTrue(state["collected"] and state["taken_over_from"].startswith("submitting-"))
        self.assertEqual(list((self.out / "api" / "batches").glob("submitting-*.json")), [])
        self.assertEqual(self.statuses(), ["ok", "ok", "ok", "ok", "too_long"])

    @unittest.skipUnless(HAVE_SDK, "needs the anthropic package")
    def test_a_failed_upload_is_sent_once_more(self):
        self.client.upload_fails = "before"  # nothing reached the API
        with self.assertRaises(ConnectionError):
            self.run_cli("review", "--yes", "--poll-seconds", "0")
        self.assertEqual(len(self.client.batches), 0)
        self.assertEqual(self.run_cli("review", "--yes", "--poll-seconds", "0"), 0)
        self.assertEqual(len(self.client.batches), 1)
        self.assertEqual(self.statuses(), ["ok", "ok", "ok", "ok", "too_long"])

    @unittest.skipUnless(HAVE_SDK, "needs the anthropic package")
    def test_a_higher_output_limit_redoes_only_the_cut_off_reviews(self):
        self.client.cut_off = ("context three", 150)
        self.assertEqual(self.run_cli("review", "--mode", "direct", "--yes", "--max-tokens", "100"), 0)
        self.assertEqual(len(self.client.sent), 3)
        self.assertTrue(self.review("L03_file")["truncated"])
        kept = self.review("L01_min")["reply"]["message_id"]

        self.assertEqual(self.run_cli("review", "--mode", "direct", "--yes", "--max-tokens", "100"), 0)
        self.assertEqual(len(self.client.sent), 3)  # the same limit would cut it off again: not sent

        self.assertEqual(self.run_cli("review", "--mode", "direct", "--yes", "--max-tokens", "200"), 0)
        self.assertEqual(len(self.client.sent), 4)  # only the cut-off review
        self.assertIn("context three", self.client.sent[-1]["messages"][0]["content"])
        self.assertEqual((self.review("L03_file")["status"], self.review("L03_file")["truncated"]), ("ok", False))
        self.assertEqual(self.review("L01_min")["reply"]["message_id"], kept)  # the complete review is the same reply

    def test_only_one_process_works_in_a_run_folder(self):
        run = cot.Run(cot.build_parser().parse_args(["review", *self.base]))
        with run.locked():
            with self.assertRaises(SystemExit):
                self.run_cli("review", "--mode", "direct", "--yes")
        self.assertEqual(self.client.sent, [])

    @unittest.skipUnless(HAVE_SDK, "needs the anthropic package")
    def test_a_rebuilt_dataset_is_flagged_and_only_changed_cells_are_sent(self):
        self.assertEqual(self.run_cli("review", "--mode", "direct", "--yes"), 0)
        run = cot.Run(cot.build_parser().parse_args(["plan", *self.base]))
        self.assertEqual(run.changes_since_last_run(), [])
        append_record(self.root / "contexts.jsonl", {"id": "o__r__3", "index": 2, "status": "ok", "known_paths": [],
                                                     "levels": {"L01_min": {"text": "a new pr", "input_tokens": 60}}})
        changes = cot.Run(cot.build_parser().parse_args(["plan", *self.base])).changes_since_last_run()
        self.assertEqual(len(changes), 1)
        self.assertIn("contexts.jsonl changed", changes[0])
        self.assertEqual(self.run_cli("review", "--mode", "direct", "--yes"), 0)
        self.assertEqual(len(self.client.sent), 4)  # the three saved reviews are reused; one new cell is sent
        self.assertEqual(cot.Run(cot.build_parser().parse_args(["plan", *self.base, "--effort", "high",
                                                                "--run-name", self.out.name])).changes_since_last_run()[0],
                         "this folder's replies were made with effort 'medium', not 'high'; none of them can be reused")

    @unittest.skipUnless(HAVE_SDK, "needs the anthropic package")
    def test_collect_again_restores_deleted_replies_without_paying(self):
        self.assertEqual(self.run_cli("review", "--yes", "--poll-seconds", "0"), 0)
        shutil.rmtree(self.out / "api" / "replies")
        self.assertEqual(self.run_cli("collect", "--again"), 0)
        self.assertEqual(len(list((self.out / "api" / "replies").rglob("*.json"))), 3)

        def no_client():
            raise AssertionError("everything is answered again: nothing may be sent")
        self.assertEqual(self.run_cli("review", "--yes", client_factory=no_client), 0)
        self.assertEqual(len(self.client.batches), 1)

    @unittest.skipUnless(HAVE_SDK, "needs the anthropic package")
    def test_an_empty_credit_balance_stops_a_direct_run_without_losing_or_rejecting_anything(self):
        self.client.no_credit_at = 2  # the included monthly credit runs out after the first review
        self.assertEqual(self.run_cli("review", "--mode", "direct", "--yes", "--workers", "1"), 2)
        self.assertEqual(len(self.client.answered), 1)
        self.assertEqual(len(self.client.sent), 2)  # the run stopped at the refusal instead of trying every request
        self.assertFalse((self.out / "api" / "rejected").exists())  # an unpaid request is not a bad request

        self.client.no_credit_at = None  # next month's credit has arrived
        self.assertEqual(self.run_cli("review", "--mode", "direct", "--yes", "--workers", "1"), 0)
        self.assertEqual(len(set(self.client.answered)), 3)
        self.assertEqual(len(self.client.answered), 3)  # the review paid for last month was not bought again
        self.assertEqual(self.statuses(), ["ok", "ok", "ok", "ok", "too_long"])

    @unittest.skipUnless(HAVE_SDK, "needs the anthropic package")
    def test_credit_running_out_inside_a_batch_sends_only_the_unprocessed_requests_again(self):
        self.assertEqual(self.run_cli("review", "--yes", "--no-wait"), 2)
        unpaid = self.client.batches["msgbatch_01"][0]["custom_id"]
        self.client.no_credit.add(unpaid)
        self.assertEqual(self.run_cli("review", "--yes", "--no-wait"), 2)  # still processing
        self.assertEqual(self.run_cli("review", "--yes", "--no-wait"), 2)  # ended: collected, and the unpaid one resent
        state = table.read_json(self.out / "api" / "batches" / "msgbatch_01.json")
        self.assertEqual((state["counts"]["saved"], state["counts"]["failed"], state["counts"]["no_credit"]), (2, 1, 1))
        self.assertFalse((self.out / "api" / "rejected").exists())
        self.assertEqual([request["custom_id"] for request in self.client.batches["msgbatch_02"]], [unpaid])

        self.client.no_credit.clear()
        self.assertEqual(self.run_cli("review", "--yes", "--poll-seconds", "0"), 0)
        self.assertEqual(len(self.client.batches), 2)
        self.assertEqual(self.statuses(), ["ok", "ok", "ok", "ok", "too_long"])

    # ---- Claude Code's print mode on the plan's sign-in, with a stand-in program

    def use_fake_claude(self):
        state = self.root / "fake_claude"
        state.mkdir()
        program = state / "claude"
        program.write_text(FAKE_CLAUDE.replace("@PYTHON@", sys.executable).replace("@REVIEW@", repr(REVIEW)), encoding="utf-8")
        program.chmod(0o755)
        patcher = mock.patch.dict(os.environ, {"FAKE_CLAUDE_STATE": str(state), "ANTHROPIC_API_KEY": "a-key-that-must-not-pay",
                                               "CLAUDE_CODE_SIMPLE": "1"})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.base += ["--via", "claude-code", "--claude-bin", str(program)]
        self.out = self.root / "out" / "claude-code_claude-opus-5-5_medium_codex-rubric_v1"

        def calls():
            path = state / "calls.jsonl"
            return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return state, calls

    def no_api(self):
        raise AssertionError("--via claude-code must not use the API")

    def test_claude_code_review_judge_and_table(self):
        state, calls = self.use_fake_claude()
        self.assertEqual(self.run_cli("review", "--yes", client_factory=self.no_api), 0)
        made, rubric = calls(), load_rubric()
        self.assertEqual(len(made), 3)
        for call in made:
            args = call["args"]
            self.assertEqual(args[args.index("--tools") + 1], "")  # no tools: the model sees only the request
            self.assertEqual(args[args.index("--system-prompt") + 1], rubric)  # replaces Claude Code's own prompt
            self.assertEqual((args[args.index("--model") + 1], args[args.index("--effort") + 1]), ("claude-opus-5-5", "medium"))
            self.assertTrue({"--print", "--safe-mode", "--no-session-persistence"} <= set(args))
            self.assertFalse(call["api_key"])  # an exported key would bill the API instead of using the plan
            self.assertEqual((call["claude_vars"], call["max_output"]), ([], "32000"))
            self.assertEqual(call["no_cache"], "1")  # nothing reads the prompt cache, and writing to it counts double
            self.assertIn("claude-outcome-table-", call["cwd"])  # an empty folder outside the repository
        self.assertEqual(sorted(call["prompt"] for call in made),
                         sorted(review_messages(text, rubric)[1]["content"] for text in ("context one", "context three", "second pr")))
        review = self.review("L01_min")
        self.assertEqual((review["status"], review["backend"]["kind"], review["reply"]["via"], review["reply"]["prompt_tokens"]),
                         ("ok", "claude-code-print", "claude-code", 100))
        self.assertEqual(self.review("L02_function")["reused_from"], "L01_min")

        self.assertEqual(self.run_cli("review", "--yes", client_factory=self.no_api), 0)
        self.assertEqual(len(calls()), 3)  # nothing is reviewed twice

        self.assertEqual(self.run_cli("judge", "--judge", "claude", "--yes", client_factory=self.no_api), 0)
        made = calls()
        self.assertEqual(len(made), 4)
        # The judge has no system prompt: an empty one, because the API refuses a blank one and leaving the
        # option out would bring in Claude Code's own agent prompt.
        self.assertEqual(made[-1]["args"][made[-1]["args"].index("--system-prompt") + 1], "")
        rows = json.loads((self.out / "table.json").read_text(encoding="utf-8"))
        first = next(row for row in rows if (row["entry"], row["level"]) == ("o__r__1", "L01_min"))
        self.assertEqual((len(rows), first["covered"], first["expected"]), (5, 1, 2))
        manifest = table.read_json(self.out / "manifest.json")
        self.assertEqual(manifest["reviewer_identity"]["kind"], "claude-code-print")
        # What Claude Code adds to a request was inspected locally and recorded, with the email, folder and date masked.
        self.assertEqual((manifest["claude_code"]["version"], manifest["claude_code"]["plan"]), ("9.9.9 (Claude Code)", "max"))
        added = " ".join(text for _, text in manifest["claude_code"]["additions"])
        self.assertIn("You are a Claude agent", added)
        self.assertIn("<email>", added)
        self.assertNotIn("someone@example.com", added)
        self.assertIn("<working folder>", added)
        self.assertIn("<date>", added)

    def test_claude_code_stops_at_the_plan_limit_and_continues_later(self):
        state, calls = self.use_fake_claude()
        (state / "limit_at").write_text("2")
        self.assertEqual(self.run_cli("review", "--yes"), 2)
        self.assertEqual(len(calls()), 2)  # one review, then the limit: the run stops instead of hammering
        self.assertEqual(len(list((self.out / "api" / "replies").rglob("*.json"))), 1)
        self.assertFalse((self.out / "api" / "rejected").exists())

        (state / "limit_at").unlink()  # the limit has reset
        self.assertEqual(self.run_cli("review", "--yes"), 0)
        made = calls()
        self.assertEqual(len(made), 4)
        self.assertNotIn(made[0]["prompt"], [call["prompt"] for call in made[1:]])  # the answered review is not sent again
        self.assertEqual(self.statuses(), ["ok", "ok", "ok", "ok", "too_long"])

    def test_claude_code_stops_at_the_cap_it_was_given_and_does_not_creep_past_it(self):
        state, calls = self.use_fake_claude()
        # The 5-hour limit is 70% used after the first review and 90% after the second; the weekly one stays low.
        (state / "limits.json").write_text(json.dumps({"five_hour": [0.5, 0.2], "reset_in": 2}))
        self.assertEqual(self.run_cli("review", "--yes", "--five-hour-cap", "80"), 2)
        self.assertEqual(len(calls()), 2)  # the review that reached the cap was saved; the third was not sent
        self.assertEqual(len(list((self.out / "api" / "replies").rglob("*.json"))), 2)

        self.assertEqual(self.run_cli("review", "--yes", "--five-hour-cap", "80"), 2)
        self.assertEqual(len(calls()), 2)  # still at the cap: running again sends nothing at all

        import time
        time.sleep(2.2)  # the window has reset
        (state / "limits.json").write_text(json.dumps({"five_hour": [0.0, 0.01]}))
        self.assertEqual(self.run_cli("review", "--yes", "--five-hour-cap", "80"), 0)
        self.assertEqual(len(calls()), 3)
        self.assertEqual(self.statuses(), ["ok", "ok", "ok", "ok", "too_long"])
        self.assertIn("unifiedWindows", self.review("L01_min")["reply"]["limits"])  # the limit report is kept with the reply

    def test_claude_code_stops_at_once_when_paid_extra_usage_is_used(self):
        state, calls = self.use_fake_claude()
        (state / "limits.json").write_text(json.dumps({"overage": True, "overage_status": "allowed"}))
        self.assertEqual(self.run_cli("review", "--yes"), 2)
        self.assertEqual(len(calls()), 1)  # the reply that revealed it is saved, and nothing else is sent
        self.assertEqual(len(list((self.out / "api" / "replies").rglob("*.json"))), 1)
        self.assertIn("extra usage", (self.out / "claude_outcome_table.log").read_text())

    def test_claude_code_can_wait_for_the_limit_to_reset_and_continue_by_itself(self):
        state, calls = self.use_fake_claude()
        (state / "limit_once").write_text("2")  # the second request hits the limit, which resets a second later
        (state / "limits.json").write_text(json.dumps({"reset_in": 1}))
        self.assertEqual(self.run_cli("review", "--yes", "--wait-on-limit", "--limit-margin-seconds", "0"), 0)
        made = calls()
        self.assertEqual(len(made), 4)  # three reviews and the one refusal; no second start by hand
        self.assertEqual(len({call["prompt"] for call in made}), 3)
        self.assertEqual(self.statuses(), ["ok", "ok", "ok", "ok", "too_long"])

    def test_claude_code_sends_nothing_after_the_stop_time(self):
        state, calls = self.use_fake_claude()
        self.assertEqual(self.run_cli("review", "--yes", "--until", "2020-01-01 00:00"), 2)
        self.assertEqual(calls(), [])
        self.assertEqual(self.run_cli("review", "--yes", "--until", "2099-01-01 00:00"), 0)
        self.assertEqual(len(calls()), 3)

        import time
        from datetime import datetime, timedelta
        soon = datetime.now() + timedelta(minutes=5)
        self.assertAlmostEqual(cot.parse_until(soon.strftime("%H:%M")), soon.replace(second=0, microsecond=0).timestamp(), delta=1)
        earlier = datetime.now() - timedelta(minutes=5)  # a clock time already past today means tomorrow
        self.assertGreater(cot.parse_until(earlier.strftime("%H:%M")), time.time() + 23 * 3600)
        with self.assertRaises(SystemExit):
            cot.parse_until("tomorrow")

    def test_claude_code_is_refused_if_it_would_give_the_model_tools(self):
        state, calls = self.use_fake_claude()
        (state / "tools_leak").touch()  # as if a future version ignored --tools ""
        with self.assertRaises(SystemExit) as stopped:
            self.run_cli("review", "--yes")
        self.assertIn("tools", str(stopped.exception))
        self.assertEqual(calls(), [])  # nothing was sent

    def test_what_claude_code_adds_is_the_same_whatever_the_request(self):
        state, calls = self.use_fake_claude()
        program = self.base[self.base.index("--claude-bin") + 1]
        first, second = cot.code_additions(program), cot.code_additions(program, effort="high")
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual((first["tools"], len(first["texts"])), ([], 4))
        self.assertEqual(calls(), [])  # inspecting spends nothing

    def test_claude_code_must_be_signed_in(self):
        state, calls = self.use_fake_claude()
        (state / "signed_out").touch()
        with self.assertRaises(SystemExit) as stopped:
            self.run_cli("review", "--yes")
        self.assertIn("code-login", str(stopped.exception))
        self.assertEqual(calls(), [])

    def test_an_unfinished_upload_holds_its_requests_back(self):
        run = cot.Run(cot.build_parser().parse_args(["plan", *self.base]))
        rids = list(run.review_requests())
        table.write_json(run.batches / "submitting-1-0.json", {"kind": "review", "requests": rids[:2]})
        table.write_json(run.batches / "msgbatch_09.json", {"id": "msgbatch_09", "requests": rids[2:], "collected": True})
        self.assertEqual(cot.in_flight(run.batches), set(rids[:2]))


if __name__ == "__main__":
    unittest.main()
