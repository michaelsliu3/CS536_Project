"""Tests for notebooks/future_work/claude_outcome_table.py with a stand-in API client: no network,
no cost. The tests that use the SDK's request types are skipped when `anthropic` is not installed."""
import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from src.review_outcomes import table
from src.review_outcomes.prompts import load_rubric, review_messages
from src.tracer_context.dataset import append_record

SCRIPT = Path(__file__).resolve().parents[1] / "notebooks" / "future_work" / "claude_outcome_table.py"
spec = importlib.util.spec_from_file_location("claude_outcome_table", SCRIPT)
cot = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = cot
spec.loader.exec_module(cot)

HAVE_SDK = importlib.util.find_spec("anthropic") is not None
REFS = [
    {"note": "unhashable value passed to cached warning_once", "path": "p.py", "from_line": 249, "to_line": 249},
    {"note": "use %d for token count", "path": "p.py", "from_line": 218, "to_line": 224, "is_ai_comment": True},
]
REVIEW = json.dumps({"findings": [{"title": "Dict passed to cached warning_once", "body": "raises TypeError",
                                   "confidence_score": 0.8, "priority": 1,
                                   "code_location": {"absolute_file_path": "/repo/p.py",
                                                     "line_range": {"start": 249, "end": 249}}}],
                     "overall_correctness": "patch is incorrect"})


def message(text, stop_reason="end_turn", input_tokens=120, output_tokens=40, **extra):
    """A stand-in for the SDK's Message: a thinking block (empty by default on Opus) and a text block."""
    content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(content=content, stop_reason=stop_reason, model="claude-opus-5-5", id="msg_test",
                           usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens), **extra)


class FakeClient:
    """Answers reviews with REVIEW and judge prompts with "[1]". Batches finish on the second check."""

    def __init__(self):
        self.messages = SimpleNamespace(stream=self._stream, count_tokens=self._count,
                                        batches=SimpleNamespace(create=self._create, retrieve=self._retrieve,
                                                                results=self._results))
        self.sent, self.batches, self.checks, self.options, self.invalid = [], {}, {}, [], set()

    def with_options(self, **options):
        self.options.append(options)
        return self

    def answer(self, params):
        self.sent.append(params)
        return message(REVIEW if "system" in params else "1. Match\n[1]")

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
        batch_id = f"msgbatch_{len(self.batches) + 1:02d}"
        self.batches[batch_id] = list(requests)
        return SimpleNamespace(id=batch_id, processing_status="in_progress")

    def _retrieve(self, batch_id):
        self.checks[batch_id] = self.checks.get(batch_id, 0) + 1
        ended = self.checks[batch_id] >= 2
        counts = SimpleNamespace(processing=0 if ended else len(self.batches[batch_id]), succeeded=0, errored=0)
        return SimpleNamespace(id=batch_id, processing_status="ended" if ended else "in_progress", request_counts=counts)

    def _results(self, batch_id):
        for request in reversed(self.batches[batch_id]):  # results come back in any order
            if request["custom_id"] in self.invalid:
                error = {"type": "error", "error": {"type": "invalid_request_error", "message": "prompt is too long"}}
                yield SimpleNamespace(custom_id=request["custom_id"], result=SimpleNamespace(type="errored", error=error))
            else:
                yield SimpleNamespace(custom_id=request["custom_id"],
                                      result=SimpleNamespace(type="succeeded", message=self.answer(request["params"])))


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
        others = [cot.ClaudeSpec(effort="high"), cot.ClaudeSpec(model="claude-sonnet-5-5"),
                  cot.ClaudeSpec(max_tokens=1000), cot.ClaudeSpec(replicate=1)]
        self.assertEqual(len({rid, *(other.request_id(messages) for other in others),
                              spec.request_id(review_messages("another context", "RUBRIC"))}), 6)
        self.assertEqual(spec.identity["effort"], "medium")

    def test_reply_from_message(self):
        reply = cot.reply_from_message(message("the review"), seconds=1.5)
        self.assertEqual((reply["text"], reply["finish_reason"], reply["prompt_tokens"], reply["completion_tokens"]),
                         ("the review", "stop", 120, 40))
        self.assertEqual((reply["served_model"], reply["via"], reply["refusal"]), ("claude-opus-5-5", "direct", None))
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
                     "--out-root", str(self.root / "out")]
        self.out = self.root / "out" / "claude-opus-5-5_medium_codex-rubric_v1"

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *argv, client_factory=None):
        with contextlib.redirect_stdout(io.StringIO()):  # the script's progress lines
            return cot.main([argv[0], *self.base, *argv[1:]], client_factory=client_factory or (lambda: self.client))

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
        review = table.read_json(self.out / "reviews" / "o__r__1" / "L01_min.json")
        self.assertEqual((review["status"], review["backend"]["model"], review["reply"]["served_model"]),
                         ("ok", "claude-opus-5-5", "claude-opus-5-5"))
        self.assertEqual(review["parsed"]["findings"][0]["path"], "p.py")
        self.assertEqual(table.read_json(self.out / "reviews" / "o__r__1" / "L02_function.json")["reused_from"], "L01_min")
        self.assertEqual(table.read_json(self.out / "reviews" / "o__r__1" / "L10_all.json")["status"], "too_long")

        self.assertEqual(self.run_cli("review", "--mode", "direct", "--yes"), 0)
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

        self.assertEqual(self.run_cli("table"), 0)  # rebuilds from the saved records, no API call
        self.assertEqual(len(self.client.sent), 4)

    @unittest.skipUnless(HAVE_SDK, "needs the anthropic package")
    def test_batch_review_resumes_and_remembers_rejections(self):
        self.assertEqual(self.run_cli("review", "--yes", "--no-wait"), 2)  # submitted, not finished
        self.assertEqual(len(self.client.batches), 1)
        self.assertEqual(self.client.options[-1], {"timeout": 1800, "max_retries": 0})  # uploads are never retried
        requests = self.client.batches["msgbatch_01"]
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(cot.REQUEST_ID.match(request["custom_id"]) for request in requests))
        self.assertEqual(table.read_json(self.out / "reviews" / "o__r__1" / "L01_min.json")["status"], "error")  # pending

        self.client.invalid.add(requests[0]["custom_id"])
        self.assertEqual(self.run_cli("review", "--yes", "--poll-seconds", "0"), 0)  # collects; nothing new is sent
        self.assertEqual(len(self.client.batches), 1)
        state = table.read_json(self.out / "api" / "batches" / "msgbatch_01.json")
        self.assertEqual((state["collected"], state["counts"]), (True, {"saved": 2, "rejected": 1, "failed": 0}))
        statuses = sorted(record["status"] for record in
                          (table.read_json(path) for path in (self.out / "reviews").rglob("*.json")))
        self.assertEqual(statuses.count("too_long"), 1)
        self.assertIn("error", statuses)  # the rejected request's cell, with the API's message
        rejected = next(table.read_json(path) for path in (self.out / "reviews").rglob("*.json")
                        if "rejected this request" in (table.read_json(path).get("error") or ""))
        self.assertIn("prompt is too long", rejected["error"])

        self.assertEqual(self.run_cli("review", "--yes", "--poll-seconds", "0"), 0)  # the rejection is not resent
        self.assertEqual(len(self.client.batches), 1)

    def test_an_unfinished_upload_holds_its_requests_back(self):
        run = cot.Run(cot.build_parser().parse_args(["plan", *self.base]))
        rids = list(run.review_requests())
        table.write_json(run.batches / "submitting-1-0.json", {"kind": "review", "requests": rids[:2]})
        table.write_json(run.batches / "msgbatch_09.json", {"id": "msgbatch_09", "requests": rids[2:], "collected": True})
        self.assertEqual(cot.in_flight(run.batches), set(rids[:2]))


if __name__ == "__main__":
    unittest.main()
