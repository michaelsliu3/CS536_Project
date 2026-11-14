import json
import logging
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from src.review_outcomes.backends import OpenAIChat
from src.review_outcomes.judge import parse_judge_answer, score_review
from src.review_outcomes.parse import normalize_path, parse_review
from src.review_outcomes import retry as retry_rule
from src.review_outcomes.prompts import (
    FOLLOW_UP_REQUEST, REVIEW_REQUEST, RUBRIC_PATH, judge_prompt, load_rubric, prompt_sha256, review_messages,
)
from src.review_outcomes.retry import RetryPolicy
from src.review_outcomes.table import (
    build_table, discover_cells, plan_reviews, run_judgments, run_reviews, summarize_by_level, write_outputs,
)
from src.tracer_context.dataset import append_record

# The roadmap's Section 8.5.3 example: vllm#17425's reference layout, with short notes.
REFS = [
    {"note": "lru_cache keys on args, print once breaks", "path": "vllm/logger.py", "from_line": 55, "to_line": 58, "is_ai_comment": True},
    {"note": "cache can grow without bound", "path": "vllm/logger.py", "from_line": 55, "to_line": 58, "is_ai_comment": True},
    {"note": "does warning_once accept args", "path": "w.py", "from_line": 718, "to_line": 721, "is_ai_comment": True},
    {"note": "two spaces before noqa", "path": "w.py", "from_line": 719, "to_line": 719, "is_ai_comment": True},
    {"note": "two spaces before noqa", "path": "q.py", "from_line": 465, "to_line": 465, "is_ai_comment": True},
    {"note": "use %d for token count", "path": "p.py", "from_line": 218, "to_line": 224, "is_ai_comment": True},
    {"note": "use %d for token count", "path": "p.py", "from_line": 243, "to_line": 249, "is_ai_comment": True},
    {"note": "unhashable value passed to cached warning_once", "path": "p.py", "from_line": 249, "to_line": 249, "is_ai_comment": False},
]
GENERATED = [
    {"note": "G1 dict passed to cached warning_once raises TypeError", "path": "p.py", "from_line": 249, "to_line": 249},
    {"note": "G2 same unhashable problem in encoder path", "path": "p.py", "from_line": 224, "to_line": 224},
    {"note": "G3 caching on args prints again", "path": "vllm/logger.py", "from_line": 56, "to_line": 56},
    {"note": "G4 split the long warning string", "path": "w.py", "from_line": 718, "to_line": 718},
    {"note": "G5 anything", "path": "config.py", "from_line": 400, "to_line": 400},
]
# Judge decisions for the example: (generated tag, file) -> 1-based numbers within that file.
DECISIONS = {("G1", "p.py"): [3], ("G2", "p.py"): [3], ("G3", "vllm/logger.py"): [1], ("G4", "w.py"): []}


class ExampleJudge:
    identity = {"kind": "example"}

    def __init__(self):
        self.calls = []

    def __call__(self, prompt):
        tag = prompt.split("-Generated Comment-")[1].split("] ", 1)[1].split(" ", 1)[0]
        path = prompt.split("same file: ")[1].split(")", 1)[0]
        self.calls.append((tag, path))
        return "1. reasoning ...\n" + json.dumps(DECISIONS[(tag, path)])


def codex_reply(findings, **overall):
    return json.dumps({"findings": findings, "overall_correctness": "patch is incorrect", **overall})


def finding(title, path, start, end=None):
    return {"title": title, "body": "why", "confidence_score": 0.8, "priority": 1,
            "code_location": {"absolute_file_path": path, "line_range": {"start": start, "end": end or start}}}


class PromptTests(unittest.TestCase):
    def test_rubric_is_the_pinned_codex_file_and_used_verbatim(self):
        rubric = load_rubric()
        self.assertEqual(rubric, RUBRIC_PATH.read_text(encoding="utf-8"))
        self.assertIn("## Output schema", rubric)
        messages = review_messages("CONTEXT", rubric)
        self.assertEqual(messages[0], {"role": "system", "content": rubric})
        self.assertEqual(messages[1]["content"], f"{REVIEW_REQUEST}\n\nCONTEXT")
        self.assertNotEqual(prompt_sha256(rubric), prompt_sha256(rubric + " "))

    def test_judge_prompt_lists_references_with_lines(self):
        prompt = judge_prompt("a.py", "3-4", "gen", ["r1", "r2"], ["1-2", "?"])
        self.assertIn("(all from the same file: a.py)", prompt)
        self.assertIn("[L3-4] gen", prompt)
        self.assertIn("1. [L1-2] r1\n2. [L?] r2", prompt)


class ParseTests(unittest.TestCase):
    def test_bare_fenced_thinking_and_prose_wrapped_json(self):
        reply = codex_reply([finding("[P1] Bug", "/repo/vllm/logger.py", 55, 58)])
        for text in (reply, f"```json\n{reply}\n```", f"<think>hmm</think>\n{reply}", f"Here you go:\n{reply}\nDone."):
            parsed = parse_review(text, {"vllm/logger.py"})
            self.assertEqual(parsed["status"], "ok", text[:30])
            self.assertEqual(parsed["findings"][0]["path"], "vllm/logger.py")
            self.assertEqual((parsed["findings"][0]["from_line"], parsed["findings"][0]["to_line"]), (55, 58))
            self.assertEqual(parsed["findings"][0]["note"], "[P1] Bug\n\nwhy")
        self.assertEqual(parsed["overall"]["overall_correctness"], "patch is incorrect")

    def test_malformed_empty_and_invalid_findings(self):
        self.assertEqual(parse_review("no json here")["status"], "malformed")
        self.assertEqual(parse_review('{"verdict": 1}')["status"], "malformed")
        self.assertEqual(parse_review("  ")["status"], "empty_output")
        parsed = parse_review(codex_reply([{"title": ""}, "x", finding("[P2] ok", "a.py", 3)]))
        self.assertEqual((parsed["status"], len(parsed["findings"]), parsed["invalid_findings"]), ("ok", 1, 2))
        self.assertEqual(parse_review(codex_reply([]))["findings"], [])  # an explicit empty review is valid

    def test_path_normalization(self):
        known = {"src/a.py", "a.py"}
        self.assertEqual(normalize_path("b/src/a.py", known), "src/a.py")
        self.assertEqual(normalize_path("/home/u/repo/src/a.py", known), "src/a.py")
        self.assertEqual(normalize_path("./src/a.py", known), "src/a.py")
        self.assertEqual(normalize_path("other/x.py", known), "other/x.py")
        self.assertEqual(normalize_path(None, known), "")


class JudgeTests(unittest.TestCase):
    def test_roadmap_example_scores(self):
        judge = ExampleJudge()
        score = score_review(REFS, GENERATED, judge)
        self.assertEqual(len(judge.calls), 4)  # G5's file has no references: no call
        self.assertEqual((score["expected"], score["generated"], score["covered"], score["matched_generated"]), (8, 5, 2, 2))
        self.assertEqual(score["covered_indices"], [0, 7])
        self.assertEqual([r["hit_new_gt"] for r in score["records"]], [True, False, True, False, False])
        self.assertAlmostEqual(score["recall"], 0.25)
        self.assertAlmostEqual(score["precision"], 0.4)
        self.assertAlmostEqual(round(score["balanced"], 3), 0.308)

    def test_answer_parsing_and_judge_errors(self):
        self.assertEqual(parse_judge_answer("1. x\n[1, 9]\nfinal [2, 3]", 3), [2, 3])
        self.assertEqual(parse_judge_answer("no list", 3), [])

        def broken(prompt):
            raise TimeoutError("judge down")
        score = score_review(REFS, GENERATED[:1], broken)
        self.assertEqual((score["judge_errors"], score["covered"]), (1, 0))
        self.assertIn("TimeoutError", score["records"][0]["judge_error"])


class TableTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.contexts = self.root / "context_levels.jsonl"
        levels = {name: {"text": f"context for {name}", "input_tokens": tokens}
                  for name, tokens in (("L01_min", 100), ("L02_function", 900))}
        append_record(self.contexts, {"id": "o__r__1", "index": 0, "status": "ok", "levels": levels,
                                      "known_paths": ["p.py"]})
        append_record(self.contexts, {"id": "o__r__2", "index": 1, "status": "failed"})
        self.references = {"o__r__1": {"project_main_language": "Python", "comments": REFS}}

    def tearDown(self):
        self._tmp.cleanup()

    def test_reviews_resume_and_table(self):
        cells = discover_cells(self.contexts)  # the failed PR has no levels and is left out
        self.assertEqual([(c.entry, c.level, c.input_tokens) for c in cells],
                         [("o__r__1", "L01_min", 100), ("o__r__1", "L02_function", 900)])
        self.assertEqual(discover_cells(self.contexts, levels=["L02_function"])[0].text, "context for L02_function")
        calls = []

        def reviewer(messages):
            calls.append(messages[1]["content"])
            return {"text": codex_reply([finding("G1 dict passed to cached warning_once", "/x/p.py", 249)]),
                    "finish_reason": "stop", "prompt_tokens": 10, "completion_tokens": 5, "seconds": 0.1}
        reviewer.identity = {"kind": "fake"}
        out = self.root / "out"
        reviews = run_reviews(cells, reviewer, "RUBRIC", out, max_input_tokens=500)
        self.assertEqual([r["status"] for r in reviews], ["ok", "too_long"])
        self.assertEqual(reviews[0]["parsed"]["findings"][0]["path"], "p.py")
        again = run_reviews(cells, reviewer, "RUBRIC", out, max_input_tokens=500)
        self.assertEqual(len(calls), 1)  # second run reused the saved review
        self.assertEqual(again[0]["key"], reviews[0]["key"])

        judge = lambda prompt: "[3]"  # noqa: E731
        judge.identity = {"kind": "fixed"}
        judgments = run_judgments(reviews, self.references, judge, out)
        self.assertEqual(len(judgments), 1)  # the too_long cell has no review to score
        rows, coverage = build_table(reviews, judgments, self.references)
        row = rows[0]
        self.assertEqual((row["covered"], row["expected"], row["human_covered"], row["human_expected"]), (1, 8, 1, 1))
        self.assertEqual(rows[1]["review_status"], "too_long")
        self.assertEqual(sum(c["covered"] for c in coverage), 1)
        (out / "table.jsonl").write_text("stale\n", encoding="utf-8")  # an older run's file
        write_outputs(out, rows, coverage)
        self.assertEqual(json.loads((out / "table.json").read_text(encoding="utf-8")), rows)  # one JSON array
        self.assertFalse((out / "table.jsonl").exists())
        self.assertTrue((out / "table.csv").is_file() and (out / "coverage.csv").is_file())
        summary = summarize_by_level(rows)
        self.assertEqual([s["level"] for s in summary], ["L01_min", "L02_function"])
        self.assertAlmostEqual(summary[0]["recall_pooled"], 0.125)
        self.assertEqual(summary[1]["statuses"], {"too_long": 1})

    def test_run_log_records_every_call_problem_and_summary(self):
        from src.review_outcomes import table

        levels = {f"L{i:02d}": {"text": f"context {i}", "input_tokens": 100 * i} for i in range(1, 6)}
        contexts = self.root / "five.jsonl"
        append_record(contexts, {"id": "o__r__9", "index": 0, "status": "ok", "levels": levels, "known_paths": ["p.py"]})
        cells = discover_cells(contexts)

        def reviewer(messages):
            text = messages[1]["content"]
            if text.endswith("context 2"):
                raise RuntimeError("HTTP 500: server fell over")
            reply = {"text": codex_reply([finding("a finding", "p.py", 1)]), "finish_reason": "stop",
                     "prompt_tokens": 10, "completion_tokens": 5, "seconds": 0.1}
            if text.endswith("context 3"):
                reply.update(text="not json at all")
            if text.endswith("context 4"):
                reply.update(text='{"findings": [', finish_reason="length", completion_tokens=4096)
            return reply
        reviewer.identity = {"kind": "fake"}
        out = self.root / "logged"
        path = table.start_run_log(out)
        self.addCleanup(lambda: [(table.log.removeHandler(h), h.close()) for h in list(table.log.handlers)])
        with mock.patch.object(table, "SUMMARY_EVERY", 0):
            reviews = run_reviews(cells, reviewer, "RUBRIC", out, max_input_tokens=450)
        text = path.read_text(encoding="utf-8")
        self.assertEqual(path.parent, out / "logs")
        self.assertIn("review stage: 5 cells from 1 entries, 5 distinct inputs; 0 already saved, 1 over the limit of 450 "
                      "input tokens, 4 to send (1,000 input tokens) with concurrency 1", text)
        self.assertIn("o__r__9 L01: ok, 100 input tokens (prompt 10), 5 output tokens, 0.1s, finish=stop, 1 findings", text)
        self.assertIn("ERROR   review 2/4 o__r__9 L02: FAILED, 200 input tokens: RuntimeError: HTTP 500: server fell over", text)
        self.assertIn("WARNING o__r__9 L03: the reply is malformed", text)
        self.assertIn("WARNING o__r__9 L04: the reply hit the output limit (4096 tokens)", text)
        self.assertIn("o__r__9 L05: too long, not sent (500 input tokens > 450)", text)
        self.assertIn("progress: 4/4 calls (100.0%), 1,000 of 1,000 input tokens", text)
        self.assertIn("review stage finished: 5 cells", text)
        self.assertIn("1 failed and will be retried by the next run", text)
        self.assertTrue(reviews[0]["started_at"] <= reviews[0]["finished_at"])
        self.assertNotIn("started_at", reviews[4])  # never sent

        calls_before = text.count("FAILED")
        again = run_reviews(cells, reviewer, "RUBRIC", out, max_input_tokens=450)  # only the failed call is retried
        text = path.read_text(encoding="utf-8")
        self.assertIn("3 already saved, 1 over the limit of 450 input tokens, 1 to send (200 input tokens)", text)
        self.assertEqual(text.count("FAILED"), calls_before + 1)
        self.assertEqual(again[0], reviews[0])  # a saved review is returned unchanged, timestamps included

        references = {"o__r__9": {"project_main_language": "Python", "comments": REFS}}
        run_judgments(again, references, ExampleJudge(), out)
        text = path.read_text(encoding="utf-8")
        self.assertIn("judge stage: 3 of 5 cells have a review to score", text)
        self.assertIn("judge stage finished: 3 cells scored", text)

    def test_review_script_selects_prs_and_reads_the_notebook_profile(self):
        import importlib.util
        import sys

        from src.review_outcomes.profiles import read_notebook_profile
        from src.review_outcomes.table import Cell

        path = Path(__file__).resolve().parents[1] / "scripts" / "outcome" / "run_reviews.py"
        spec = importlib.util.spec_from_file_location("run_reviews", path)
        script = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = script
        spec.loader.exec_module(script)
        cells = [Cell(entry, level, f"{entry} {level}", 10) for entry in ("b", "a", "c") for level in ("L01", "L02")]
        self.assertEqual([c.entry for c in script.select_cells(cells, 2, None)], ["b", "b", "a", "a"])  # dataset order
        self.assertEqual([c.entry for c in script.select_cells(cells, None, ["c"])], ["c", "c"])
        self.assertEqual(len(script.select_cells(cells, None, None)), 6)
        with self.assertRaises(SystemExit):
            script.select_cells(cells, None, ["missing"])
        reviewer = read_notebook_profile("cloud")["reviewer"]
        self.assertEqual(script.make_reviewer(reviewer).identity["model"], "Qwen/Qwen3.8-27B")
        with self.assertRaises(ValueError):
            read_notebook_profile("no-such-profile")

    def test_too_long_cells_are_reviewed_once_the_limit_allows(self):
        cells, calls = discover_cells(self.contexts), []

        def reviewer(messages):
            calls.append(messages[1]["content"])
            return {"text": codex_reply([]), "finish_reason": "stop", "prompt_tokens": 10,
                    "completion_tokens": 5, "seconds": 0.1}
        reviewer.identity = {"kind": "fake"}
        out = self.root / "limit_out"
        first = run_reviews(cells, reviewer, "RUBRIC", out, max_input_tokens=500)
        self.assertEqual(([r["status"] for r in first], len(calls)), (["ok", "too_long"], 1))
        again = run_reviews(cells, reviewer, "RUBRIC", out, max_input_tokens=500)
        self.assertEqual(([r["status"] for r in again], len(calls)), (["ok", "too_long"], 1))  # still too long
        raised = run_reviews(cells, reviewer, "RUBRIC", out, max_input_tokens=1000)
        self.assertEqual(([r["status"] for r in raised], len(calls)), (["ok", "ok"], 2))  # only L02 is new

    def test_server_address_is_not_part_of_the_cache_key(self):
        a = OpenAIChat("http://localhost:8000/v1", "Qwen/Qwen3.8-27B", model_revision="r1")
        b = OpenAIChat("http://10.0.0.5:9000/v1/", "Qwen/Qwen3.8-27B", model_revision="r1")
        self.assertEqual(a.identity, b.identity)
        self.assertNotIn("base_url", a.identity)
        self.assertEqual(b.url, "http://10.0.0.5:9000/v1/chat/completions")
        self.assertNotEqual(a.identity, OpenAIChat("http://localhost:8000/v1", "Qwen/Qwen3.8-27B",
                                                   model_revision="r2").identity)

    def test_identical_levels_are_reviewed_and_judged_once(self):
        levels = {name: {"text": text, "input_tokens": 100}
                  for name, text in (("L05_callers1", "same"), ("L06_tests1", "same"), ("L07_agent", "more"))}
        contexts = self.root / "same.jsonl"
        append_record(contexts, {"id": "o__r__1", "index": 0, "status": "ok", "levels": levels, "known_paths": []})
        calls = []

        def reviewer(messages):
            calls.append(messages[1]["content"])
            return {"text": codex_reply([finding("G1 dict passed to cached warning_once", "p.py", 249)]),
                    "finish_reason": "stop", "prompt_tokens": 10, "completion_tokens": 5, "seconds": 0.1}
        reviewer.identity = {"kind": "fake"}
        out = self.root / "same_out"
        reviews = run_reviews(discover_cells(contexts), reviewer, "RUBRIC", out, concurrency=2)
        self.assertEqual(len(calls), 2)  # "same" once, "more" once
        self.assertEqual([(r["level"], r.get("reused_from")) for r in reviews],
                         [("L05_callers1", None), ("L06_tests1", "L05_callers1"), ("L07_agent", None)])
        self.assertTrue((out / "reviews" / "o__r__1" / "L06_tests1.json").is_file())
        judge_calls = []

        def judge(prompt):
            judge_calls.append(prompt)
            return "[3]"
        judge.identity = {"kind": "fixed"}
        judgments = run_judgments(reviews, self.references, judge, out)
        self.assertEqual(len(judge_calls), 2)  # one finding in each of the two different reviews
        self.assertEqual([j.get("reused_from") for j in judgments], [None, "L05_callers1", None])
        rows, _ = build_table(reviews, judgments, self.references)
        self.assertEqual([r["reused_from"] for r in rows], [None, "L05_callers1", None])


def reply(text, finish="stop", prompt=1810, completion=50):
    return {"text": text, "finish_reason": finish, "prompt_tokens": prompt, "completion_tokens": completion, "seconds": 0.1}


CUT = reply("Let me trace the callers. So the", "length", completion=4096)  # reasoning that ran out of room
PROSE = reply("The patch is correct.")                                       # finished, but no JSON
TOOL = reply("Let me look.\n<tool_call>\n<function=Bash>\n</function>\n</tool_call>")
GOOD = reply(codex_reply([finding("G1 a real bug", "p.py", 3)]))
EMPTY = reply(codex_reply([]))
POLICY = RetryPolicy(longer_max_tokens=16000, follow_up=True, context_window=262144)


class ScriptedReviewer:
    """A reviewer whose replies come from a script keyed by (level, kind of call), where the kind is
    "first", "longer" (the first request again with a larger limit) or "follow_up". A list is used
    up one reply per call; an exception is raised. Every call is recorded."""

    max_tokens, identity = 4096, {"kind": "fake", "max_tokens": 4096}

    def __init__(self, script):
        self.script, self.calls, self.requests = script, [], []

    def __call__(self, messages, max_tokens=None):
        level = messages[1]["content"].split()[-1]
        kind = "follow_up" if len(messages) == 4 else "longer" if max_tokens else "first"
        self.calls.append((level, kind, max_tokens))
        self.requests.append(messages)
        answer = self.script[(level, kind)]
        if isinstance(answer, list):
            answer = answer.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)


class RetryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.out = self.root / "out"
        quiet = logging.NullHandler()  # these tests make cut-off and malformed replies on purpose
        logging.getLogger("review_outcomes").addHandler(quiet)
        self.addCleanup(logging.getLogger("review_outcomes").removeHandler, quiet)

    def tearDown(self):
        self._tmp.cleanup()

    def cells(self, *levels, name="levels"):
        """One PR whose levels have the texts "context <level>"; a level may be (name, same-as level)."""
        named = [level if isinstance(level, tuple) else (level, level) for level in levels]
        contexts = self.root / f"{name}.jsonl"
        append_record(contexts, {"id": "o__r__7", "index": 0, "status": "ok", "known_paths": ["p.py"],
                                 "levels": {n: {"text": f"context {text}", "input_tokens": 100} for n, text in named}})
        return discover_cells(contexts)

    def saved(self, level):
        return json.loads((self.out / "reviews" / "o__r__7" / f"{level}.json").read_text(encoding="utf-8"))

    def test_cut_off_reply_is_sent_again_with_a_longer_limit(self):
        cells = self.cells("L01")
        reviewer = ScriptedReviewer({("L01", "first"): CUT, ("L01", "longer"): GOOD})
        record = run_reviews(cells, reviewer, "RUBRIC", self.out, retry=POLICY)[0]
        self.assertEqual(reviewer.calls, [("L01", "first", None), ("L01", "longer", 16000)])
        self.assertEqual(reviewer.requests[1], reviewer.requests[0])  # the same request, only the limit differs
        self.assertEqual((record["status"], record["truncated"], len(record["parsed"]["findings"])), ("ok", False, 1))
        self.assertEqual(record["reply"], GOOD)  # the last reply is the one in force
        self.assertEqual(record["retry"]["policy"], POLICY.identity)
        self.assertEqual(record["retry"]["first"], {"status": "malformed", "reply": CUT, "truncated": True})
        step = record["retry"]["steps"][0]
        self.assertEqual((step["step"], step["max_tokens"], step["status"], step["truncated"], step["reply"]),
                         ("longer_output", 16000, "ok", False, GOOD))
        self.assertEqual(retry_rule.steps_taken(record), "longer_output")
        self.assertEqual(self.saved("L01"), record)
        # The cache key covers the first call only: it is the key a run without the policy gives.
        plain = run_reviews(cells, ScriptedReviewer({("L01", "first"): CUT}), "RUBRIC", self.root / "plain")[0]
        self.assertEqual(record["key"], plain["key"])
        self.assertNotIn("retry", plain)
        again = run_reviews(cells, reviewer, "RUBRIC", self.out, retry=POLICY)  # nothing left to do
        self.assertEqual((again, len(reviewer.calls)), ([record], 2))

    def test_reply_without_json_gets_one_follow_up(self):
        cells = self.cells("L01", "L02")
        reviewer = ScriptedReviewer({("L01", "first"): PROSE, ("L01", "follow_up"): EMPTY,
                                     ("L02", "first"): TOOL, ("L02", "follow_up"): GOOD})
        first, second = run_reviews(cells, reviewer, "RUBRIC", self.out, retry=POLICY)
        self.assertEqual(reviewer.calls, [("L01", "first", None), ("L01", "follow_up", 4096),
                                          ("L02", "first", None), ("L02", "follow_up", 4096)])
        request = reviewer.requests[1]  # the conversation so far, the reply, then the fixed message
        self.assertEqual(request[:2], reviewer.requests[0])
        self.assertEqual(request[2:], [{"role": "assistant", "content": PROSE["text"]},
                                       {"role": "user", "content": FOLLOW_UP_REQUEST}])
        self.assertIn("cannot run commands", FOLLOW_UP_REQUEST)
        self.assertEqual((first["status"], first["parsed"]["findings"], retry_rule.steps_taken(first)),
                         ("ok", [], "follow_up"))
        self.assertEqual((second["status"], len(second["parsed"]["findings"])), ("ok", 1))
        self.assertEqual(second["retry"]["first"]["reply"], TOOL)

    def test_steps_run_once_each_in_order_and_the_last_reply_decides(self):
        cells = self.cells("L01", "L02", "L03", "L04")
        unfinished = reply("Finding 1 is real. Let me write the findings.", completion=9000)
        still_cut = reply("Still thinking. So the", "length", completion=16000)
        reviewer = ScriptedReviewer({
            ("L01", "first"): CUT, ("L01", "longer"): unfinished, ("L01", "follow_up"): GOOD,  # both steps
            ("L02", "first"): CUT, ("L02", "longer"): still_cut,                                # cut off again
            ("L03", "first"): PROSE, ("L03", "follow_up"): PROSE,                               # no JSON again
            ("L04", "first"): GOOD})                                                            # never retried
        records = run_reviews(cells, reviewer, "RUBRIC", self.out, retry=POLICY)
        self.assertEqual([retry_rule.steps_taken(r) for r in records],
                         ["longer_output+follow_up", "longer_output", "follow_up", None])
        self.assertEqual([(r["status"], r["truncated"]) for r in records],
                         [("ok", False), ("malformed", True), ("malformed", False), ("ok", False)])
        self.assertEqual(records[1]["parsed"]["findings"], [])  # still no answer: a review with no findings
        self.assertNotIn("retry", records[3])
        follow_up = reviewer.requests[reviewer.calls.index(("L01", "follow_up", 4096))]
        self.assertEqual(follow_up[2]["content"], unfinished["text"])  # it continues from the longer reply
        self.assertEqual(len(reviewer.calls), 4 + 2 + 1 + 1)
        run_reviews(cells, reviewer, "RUBRIC", self.out, retry=POLICY)  # a cell that still has no answer is left
        self.assertEqual(len(reviewer.calls), 8)
        for record in records:
            self.assertIsNone(retry_rule.next_step(record, POLICY))

    def test_step_is_skipped_when_the_window_has_no_room(self):
        cells = self.cells("L01", "L02", "L03")
        reviewer = ScriptedReviewer({
            ("L01", "first"): reply("So the", "length", prompt=257_699, completion=4096),  # 4,445 tokens of room
            ("L02", "first"): reply("Let me write the findings.", prompt=257_708, completion=3689),
            ("L03", "first"): reply("So the", "length", prompt=249_293, completion=4096), ("L03", "longer"): GOOD})
        records = run_reviews(cells, reviewer, "RUBRIC", self.out, retry=POLICY)
        self.assertEqual(reviewer.calls, [("L01", "first", None), ("L02", "first", None), ("L03", "first", None),
                                          ("L03", "longer", 262_144 - 249_293)])  # the limit is capped by the window
        self.assertEqual([r["status"] for r in records], ["malformed", "malformed", "ok"])
        for record, step in zip(records[:2], ("longer_output", "follow_up")):
            self.assertEqual([s["step"] for s in record["retry"]["steps"]], [step])
            self.assertIn("no room", record["retry"]["steps"][0]["skipped"])
            self.assertEqual(record["reply"], record["retry"]["first"]["reply"])  # the first reply stays in force
            self.assertIsNone(retry_rule.steps_taken(record))
        run_reviews(cells, reviewer, "RUBRIC", self.out, retry=POLICY)
        self.assertEqual(len(reviewer.calls), 4)  # a skipped step is not tried again
        unlimited = RetryPolicy(longer_max_tokens=16000, follow_up=True, context_window=None)
        self.assertEqual(retry_rule.step_limit("longer_output", {"reply": CUT}, unlimited, 4096), (16000, None))
        self.assertEqual(retry_rule.step_limit("follow_up", {"reply": PROSE}, unlimited, None), (None, None))

    def test_replies_saved_before_the_rule_are_retried_without_a_second_first_call(self):
        from src.review_outcomes import table

        cells = self.cells("L01", "L02", "L03")
        before = ScriptedReviewer({("L01", "first"): GOOD, ("L02", "first"): CUT, ("L03", "first"): TOOL})
        old = run_reviews(cells, before, "RUBRIC", self.out)  # the run before the rule existed
        self.assertEqual([r["status"] for r in old], ["ok", "malformed", "malformed"])
        untouched = (self.out / "reviews" / "o__r__7" / "L01.json").read_bytes()
        plan = plan_reviews(cells, before, "RUBRIC", self.out, retry=POLICY)
        self.assertEqual((plan["saved"], plan["send"]["cells"], plan["no_room"]), (3, 0, 0))
        self.assertEqual(plan["retry"], {"longer_output": {"cells": 1, "input_tokens": 100},
                                         "follow_up": {"cells": 1, "input_tokens": 100}})
        self.assertEqual(self.saved("L02"), old[1])  # planning wrote nothing

        after = ScriptedReviewer({("L02", "longer"): GOOD, ("L03", "follow_up"): EMPTY})
        path = table.start_run_log(self.out)
        self.addCleanup(lambda: [(table.log.removeHandler(h), h.close()) for h in list(table.log.handlers)])
        new = run_reviews(cells, after, "RUBRIC", self.out, retry=POLICY)
        self.assertEqual(after.calls, [("L02", "longer", 16000), ("L03", "follow_up", 4096)])
        self.assertEqual([r["status"] for r in new], ["ok", "ok", "ok"])
        self.assertEqual(new[0], old[0])
        self.assertEqual((self.out / "reviews" / "o__r__7" / "L01.json").read_bytes(), untouched)
        for was, now in zip(old[1:], new[1:]):
            self.assertEqual((now["key"], now["started_at"], now["finished_at"]),
                             (was["key"], was["started_at"], was["finished_at"]))
            self.assertEqual(now["retry"]["first"]["reply"], was["reply"])
        text = path.read_text(encoding="utf-8")
        self.assertIn("3 already saved, 0 over the limit of None input tokens, 0 to send (0 input tokens) with "
                      "concurrency 1; 2 saved replies to retry (1 cut off, 1 finished without the JSON; 0 more have "
                      "no room in the window); retry policy {'version': 'retry-v1'", text)
        self.assertIn("retry o__r__7 L02: longer_output with an output limit of 16000: ok, 50 output tokens", text)
        self.assertIn("retry o__r__7 L03: follow_up with an output limit of 4096: ok", text)
        self.assertIn("review 1/2 o__r__7 L02: ok, 100 input tokens (prompt 1810), 50 output tokens, 0.1s, "
                      "finish=stop, 1 findings (after longer_output)", text)
        self.assertIn("retry rule: 2 of 3 distinct replies took a retry step, 2 got a new reply; of those 2, 2 now "
                      "have an answer and 0 do not (0 still cut off). Steps: 1 longer output, 1 follow-up, "
                      "0 skipped for lack of room, 0 failed", text)

    def test_changing_or_removing_the_policy_starts_again_from_the_first_reply(self):
        cells = self.cells("L01")
        reviewer = ScriptedReviewer({("L01", "first"): CUT, ("L01", "longer"): [GOOD, EMPTY]})
        record = run_reviews(cells, reviewer, "RUBRIC", self.out, retry=POLICY)[0]
        shorter = RetryPolicy(longer_max_tokens=8000, follow_up=True, context_window=262144)
        redone = run_reviews(cells, reviewer, "RUBRIC", self.out, retry=shorter)[0]
        self.assertEqual(reviewer.calls, [("L01", "first", None), ("L01", "longer", 16000), ("L01", "longer", 8000)])
        self.assertEqual((redone["retry"]["policy"], redone["retry"]["first"]), (shorter.identity, record["retry"]["first"]))
        self.assertEqual((redone["reply"], [s["max_tokens"] for s in redone["retry"]["steps"]]), (EMPTY, [8000]))
        plain = run_reviews(cells, reviewer, "RUBRIC", self.out)[0]  # no policy: the first reply, as first saved
        self.assertEqual(len(reviewer.calls), 3)
        self.assertEqual((plain["status"], plain["reply"], plain["truncated"], "retry" in plain),
                         ("malformed", CUT, True, False))
        self.assertEqual(self.saved("L01"), plain)

    def test_failed_step_keeps_the_first_reply_and_is_tried_again(self):
        cells = self.cells("L01")
        reviewer = ScriptedReviewer({("L01", "first"): CUT,
                                     ("L01", "longer"): [RuntimeError("HTTP 500: server fell over"), GOOD]})
        failed = run_reviews(cells, reviewer, "RUBRIC", self.out, retry=POLICY)[0]
        self.assertEqual((failed["status"], failed["reply"], failed["truncated"]), ("malformed", CUT, True))
        self.assertEqual(failed["retry"]["steps"][0]["error"], "RuntimeError: HTTP 500: server fell over")
        self.assertEqual(retry_rule.next_step(failed, POLICY), "longer_output")
        fixed = run_reviews(cells, reviewer, "RUBRIC", self.out, retry=POLICY)[0]
        self.assertEqual(reviewer.calls, [("L01", "first", None), ("L01", "longer", 16000), ("L01", "longer", 16000)])
        self.assertEqual((fixed["status"], [("error" in s, s["step"]) for s in fixed["retry"]["steps"]]),
                         ("ok", [(False, "longer_output")]))  # the failed attempt is replaced, not kept

    def test_retried_review_reaches_its_copies_the_judge_and_the_table(self):
        cells = self.cells("L05", ("L06", "L05"), "L07", name="same")  # L06 has the same input as L05
        references = {"o__r__7": {"project_main_language": "Python", "comments": REFS}}
        judge_calls = []

        def judge(prompt):
            judge_calls.append(prompt)
            return "[3]"
        judge.identity = {"kind": "fixed"}
        before = ScriptedReviewer({("L05", "first"): CUT, ("L07", "first"): GOOD})
        old = run_reviews(cells, before, "RUBRIC", self.out)
        old_judgments = run_judgments(old, references, judge, self.out)
        self.assertEqual(len(judge_calls), 1)  # only L07 has a finding to judge

        after = ScriptedReviewer({("L05", "longer"): GOOD})
        new = run_reviews(cells, after, "RUBRIC", self.out, retry=POLICY)
        self.assertEqual(after.calls, [("L05", "longer", 16000)])  # once for the two identical levels
        self.assertEqual([(r["status"], r.get("reused_from"), retry_rule.steps_taken(r)) for r in new],
                         [("ok", None, "longer_output"), ("ok", "L05", "longer_output"), ("ok", None, None)])
        self.assertEqual(self.saved("L06")["retry"], new[0]["retry"])
        judgments = run_judgments(new, references, judge, self.out)
        self.assertEqual(len(judge_calls), 2)  # the retried review is judged afresh, its copy and L07 are not
        self.assertNotEqual(judgments[0]["key"], old_judgments[0]["key"])  # same review key, another answer
        self.assertEqual(new[0]["key"], old[0]["key"])
        self.assertEqual(judgments[2], old_judgments[2])
        self.assertIsNone(retry_rule.answer_id(new[2]))
        rows, _ = build_table(new, judgments, references)
        self.assertEqual([(row["retry"], row["review_status"], row["findings"]) for row in rows],
                         [("longer_output", "ok", 1), ("longer_output", "ok", 1), (None, "ok", 1)])

    def test_backend_takes_an_output_limit_for_one_call(self):
        sent = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
                                   "usage": {"prompt_tokens": 7, "completion_tokens": 1}}).encode("utf-8")

        def urlopen(request, timeout=None):
            sent.append(json.loads(request.data.decode("utf-8")))
            return Response()

        chat = OpenAIChat("http://localhost:8000/v1", "Qwen/Qwen3.8-27B", max_tokens=4096)
        identity = dict(chat.identity)
        with mock.patch("urllib.request.urlopen", urlopen):
            chat([{"role": "user", "content": "hi"}])
            chat([{"role": "user", "content": "hi"}], max_tokens=16000)
        self.assertEqual([body["max_tokens"] for body in sent], [4096, 16000])
        self.assertEqual(sent[0]["chat_template_kwargs"], {"enable_thinking": False})
        self.assertEqual((chat.identity, chat.max_tokens), (identity, 4096))  # the cache key does not move

    def test_policy_comes_from_the_notebook_profile(self):
        import importlib.util
        import sys

        from src.review_outcomes.profiles import read_notebook_profile

        self.assertEqual(RetryPolicy.from_spec(read_notebook_profile("cloud")["reviewer"]), POLICY)
        self.assertIsNone(RetryPolicy.from_spec(read_notebook_profile("local-smoke")["reviewer"]))
        self.assertEqual(POLICY.identity["version"], "retry-v1")
        self.assertNotEqual(POLICY.identity, RetryPolicy(16000, False, 262144).identity)
        self.assertIsNone(retry_rule.next_step({"status": "malformed", "truncated": True, "reply": CUT}, None))
        self.assertIsNone(retry_rule.next_step({"status": "error"}, POLICY))
        self.assertIsNone(retry_rule.next_step({"status": "malformed", "truncated": False, "reply": PROSE},
                                               RetryPolicy(longer_max_tokens=16000)))  # no follow-up in this policy

        path = Path(__file__).resolve().parents[1] / "scripts" / "outcome" / "run_reviews.py"
        spec = importlib.util.spec_from_file_location("run_reviews_for_retry", path)
        script = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = script
        spec.loader.exec_module(script)
        cells = self.cells("L01", "L02")
        run_reviews(cells, ScriptedReviewer({("L01", "first"): CUT, ("L02", "first"): PROSE}), "RUBRIC", self.out)
        plan = plan_reviews(cells, ScriptedReviewer({}), "RUBRIC", self.out, retry=POLICY)
        text = script.describe_plan(plan, POLICY, self.out)
        self.assertIn("2 cells from 1 PRs, 2 distinct inputs", text)
        self.assertIn("cut off, to send again with an output limit of up to 16000: 1 (100 input tokens)", text)
        self.assertIn("finished without the JSON, to get the follow-up message: 1 (100 input tokens)", text)
        self.assertIn("no retry policy in this profile", script.describe_plan(plan, None, self.out))


class JudgeStageTests(unittest.TestCase):
    def test_judge_script_reads_saved_reviews_and_the_profile_names_a_judge(self):
        import importlib.util
        import sys

        from src.review_outcomes.profiles import read_notebook_profile
        from src.review_outcomes.table import Cell, write_json

        folder = Path(__file__).resolve().parents[1] / "scripts" / "outcome"
        spec = importlib.util.spec_from_file_location("run_judgments", folder / "run_judgments.py")
        script = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = script
        spec.loader.exec_module(script)
        self.addCleanup(lambda: sys.path.remove(str(folder)) if str(folder) in sys.path else None)

        judge = read_notebook_profile("cloud")["judge"]  # the reviewer's model on the reviewer's server
        reviewer = read_notebook_profile("cloud")["reviewer"]
        self.assertEqual((judge["model"], judge["model_revision"], judge["base_url"]),
                         (reviewer["model"], reviewer["model_revision"], reviewer["base_url"]))
        self.assertEqual(script.make_chat(judge).identity["max_tokens"], 3000)

        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp)
            cells = [Cell("o__r__1", level, level, 10) for level in ("L01", "L02", "L03", "L04")]
            findings = [{"note": "a", "path": "p.py"}, {"note": "b", "path": "no_reference_here.py"}]
            for level, status, key in (("L01", "ok", "k1"), ("L02", "ok", "k1"), ("L03", "too_long", "k3")):
                write_json(out / "reviews" / "o__r__1" / f"{level}.json",
                           {"key": key, "entry": "o__r__1", "level": level, "status": status,
                            "parsed": {"findings": findings} if status == "ok" else None})
            reviews, missing = script.load_reviews(out, cells)
            self.assertEqual(([r["level"] for r in reviews], missing), (["L01", "L02", "L03"], ["o__r__1 L04"]))
            text = script.describe(reviews, missing, {"o__r__1": {"comments": REFS}}, out, {"kind": "fake"})
            self.assertIn("3 saved reviews, statuses {'ok': 2, 'too_long': 1}; 1 cells have no saved review", text)
            self.assertIn("1 distinct reviews to score, with 2 findings; about 1 judge calls", text)

    def test_reviews_are_judged_in_parallel_once_each_and_resume(self):
        reviews = [{"key": f"k{i % 3}", "entry": "o__r__1", "level": f"L{i:02d}", "status": "ok",
                    "parsed": {"findings": [{"note": f"G{i % 3} x", "path": "p.py", "from_line": 1, "to_line": 1}]}}
                   for i in range(6)]
        references = {"o__r__1": {"project_main_language": "Python", "comments": REFS}}
        calls = []

        def judge(prompt):
            calls.append(prompt)
            return "[3]"
        judge.identity = {"kind": "fixed"}
        with tempfile.TemporaryDirectory() as tmp:
            judgments = run_judgments(reviews, references, judge, Path(tmp), concurrency=4)
            self.assertEqual(len(calls), 3)  # six cells, three distinct reviews
            self.assertEqual([j["level"] for j in judgments], [f"L{i:02d}" for i in range(6)])
            self.assertEqual([j.get("reused_from") for j in judgments], [None, None, None, "L00", "L01", "L02"])
            again = run_judgments(reviews, references, judge, Path(tmp), concurrency=4)
            self.assertEqual((len(calls), again), (3, judgments))


if __name__ == "__main__":
    unittest.main()
