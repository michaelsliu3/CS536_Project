import json
import logging
import tempfile
import unittest
from unittest import mock
from pathlib import Path

from src.review_outcomes.backends import OpenAIChat
from src.review_outcomes.judge import parse_judge_answer, score_review
from src.review_outcomes.parse import locate_snippet, normalize_path, parse_review, parse_tagged_review
from src.review_outcomes import retry as retry_rule
from src.review_outcomes.prompts import (
    AACR_FOLLOW_UP_REQUEST, AACR_NO_CONTEXT_PATH, AACR_PROMPT_VERSION, AACR_WITH_CONTEXT_PATH, FOLLOW_UP_REQUEST,
    REVIEW_PROMPT_VERSION, REVIEW_REQUEST, RUBRIC_PATH, ReviewInput, aacr_prompt, as_prompt, judge_prompt,
    load_review_prompt, load_rubric, prompt_sha256, review_messages,
)
from src.review_outcomes.retry import RetryPolicy
from src.review_outcomes.table import (
    build_hunk_tables, build_table, discover_cells, plan_reviews, references_by_entry, run_judgments, run_reviews,
    summarize_by_level, write_outputs,
)
from src.tracer_context.build import CONTEXT_HEADING, SEPARATOR, input_sections, render_diff, split_input
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



HUNK_P = "\n".join(["diff --git a/p.py b/p.py", "index 1..2 100644", "--- a/p.py", "+++ b/p.py",
                    "@@ -247,3 +247,4 @@ def warn(d):", " a = 1", "-b = 2", "+b = warning_once(d)", "+c = 3", " d = 4"])
HUNK_W = "\n".join(["diff --git a/w.py b/w.py", "index 3..4 100644", "--- a/w.py", "+++ b/w.py",
                    "@@ -717,3 +717,3 @@", " x = 0", "-old", "+new long warning string", " y = 1"])


def tagged(*comments, end=True):
    """A reply in the paper's tags: each comment is (quoted snippet, side, note)."""
    parts = [f"<diff>\n{snippet}\n</diff>\n<side>{side}</side>\n<note>\n{note}\n</note>\n<notesplit />"
             for snippet, side, note in comments]
    return "\n".join(parts + (["<end />"] if end else []))


def hunk_level(hunk, parent=None, context="", tokens=10, same=False):
    """One stored level of a per-hunk record, with the text the builder would write."""
    pr, diff = "## Pull request\n\nTitle: Fix it\n\nWhy it is needed.", render_diff(hunk)
    text = SEPARATOR.join([pr, diff] + ([f"{CONTEXT_HEADING}{SEPARATOR}{context}"] if context else []))
    return {"parent": parent, "text": None if same else text, "input_tokens": tokens, "same_as_parent": same,
            "sections": input_sections(pr, diff, "", context)}


class AacrPromptTests(unittest.TestCase):
    def test_paper_prompt_is_pinned_and_filled_slot_by_slot(self):
        prompt = load_review_prompt("aacr-hunk")
        self.assertEqual((prompt.name, prompt.version, prompt.reply_format, prompt.follow_up),
                         ("aacr-hunk", AACR_PROMPT_VERSION, "aacr-tags", AACR_FOLLOW_UP_REQUEST))
        # The two files have the lengths of the paper's Figures 16 and 17, which differ by the context section.
        plain = AACR_NO_CONTEXT_PATH.read_text(encoding="utf-8").rstrip("\n")
        rich = AACR_WITH_CONTEXT_PATH.read_text(encoding="utf-8").rstrip("\n")
        self.assertEqual((len(plain), len(rich)), (1881, 1923))
        self.assertEqual(rich.replace("## Relevant Code Context\n\n{{ contexts }}\n\n", ""), plain)

        messages = prompt.messages("the level's whole text is not used", ReviewInput("Fix it", "Why.", "THE HUNK", ""))
        self.assertEqual([m["role"] for m in messages], ["user"])  # one message, as in the paper
        text = messages[0]["content"]
        self.assertTrue(text.startswith("You are an expert code reviewer. Your task is to review"))
        self.assertIn("**Title:** Fix it\n\n**Description:** Why.\n\n## Code Changes to Review\n\n```diff\nTHE HUNK\n```\n\n## Instructions", text)
        self.assertNotIn("Relevant Code Context", text)  # Figure 16: no context section
        self.assertTrue(text.endswith("Now provide your review:"))
        self.assertEqual(text, plain.replace("{{ pr_title }}", "Fix it").replace("{{ pr_description }}", "Why.")
                         .replace("{{ diff_hunk }}", "THE HUNK"))  # nothing but the slots is changed

        with_context = prompt.messages("", ReviewInput("T", "has {{ contexts }} in it", "H", "### changed: f\nCODE"))[0]["content"]
        self.assertIn("```diff\nH\n```\n\n## Relevant Code Context\n\n### changed: f\nCODE\n\n## Instructions", with_context)
        self.assertIn("has {{ contexts }} in it", with_context)  # slot-like text in the input is left alone
        with self.assertRaisesRegex(ValueError, "in parts"):
            prompt.messages("only the whole text")
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / "prompt.md"
            copy.write_text("{{ pr_title }} {{ pr_description }}\n", encoding="utf-8")  # no slot for the diff
            with self.assertRaisesRegex(ValueError, "slots"):
                aacr_prompt(no_context=copy)

    def test_codex_prompt_object_is_the_prompt_used_so_far(self):
        rubric = load_rubric()
        prompt = load_review_prompt("codex")
        self.assertEqual(prompt.messages("CTX"), review_messages("CTX", rubric))
        self.assertEqual((prompt.sha256, prompt.version, prompt.reply_format, prompt.follow_up),
                         (prompt_sha256(rubric), REVIEW_PROMPT_VERSION, "codex-json", FOLLOW_UP_REQUEST))
        self.assertIs(as_prompt(prompt), prompt)
        self.assertEqual(as_prompt("RUBRIC").messages("CTX"), review_messages("CTX", "RUBRIC"))  # a rubric text still works
        with self.assertRaises(ValueError):
            load_review_prompt("no-such-prompt")
        # The retry policy's identity is unchanged for the Codex prompt, and differs for the paper's.
        spec = {"retry": {"longer_max_tokens": 16_000, "follow_up": True, "context_window": 262_144}}
        self.assertEqual(RetryPolicy.from_spec(spec, prompt.follow_up).identity, RetryPolicy.from_spec(spec).identity)
        self.assertNotEqual(RetryPolicy.from_spec(spec, AACR_FOLLOW_UP_REQUEST).identity, RetryPolicy.from_spec(spec).identity)

    def test_per_hunk_json_findings_are_placed_in_the_hunks_file(self):
        reply = codex_reply([finding("in the file", "b/p.py", 5), finding("named another file", "lib/other.py", 9)])
        plain = parse_review(reply, ["p.py"])
        self.assertEqual([f["path"] for f in plain["findings"]], ["p.py", "lib/other.py"])
        self.assertNotIn("reported_path", plain["findings"][0])
        placed = parse_review(reply, ["p.py"], path="p.py")
        self.assertEqual([(f["path"], f["reported_path"], f["raw_path"]) for f in placed["findings"]],
                         [("p.py", "p.py", "b/p.py"), ("p.py", "lib/other.py", "lib/other.py")])

    def test_level_text_is_taken_apart_for_the_slots(self):
        level = hunk_level(HUNK_P, context="### changed: p.warn  [p.py:240-250]\n```python\ndef warn(d): ...\n```")
        parts = split_input(level["text"], level["sections"])
        self.assertEqual(parts, {"pr_title": "Fix it", "pr_description": "Why it is needed.", "diff": HUNK_P,
                                 "contexts": "### changed: p.warn  [p.py:240-250]\n```python\ndef warn(d): ...\n```"})
        self.assertEqual(split_input(hunk_level(HUNK_P)["text"], hunk_level(HUNK_P)["sections"])["contexts"], "")
        # The PR's other hunks come first in the context, one heading level down.
        pr, diff, other = "## Pull request\n\nTitle: T\n\n(no description)", render_diff(HUNK_P), "## Other changes in this pull request\n\nDIFF"
        text = SEPARATOR.join([pr, diff, other, f"{CONTEXT_HEADING}{SEPARATOR}BLOCK"])
        self.assertEqual(split_input(text, input_sections(pr, diff, other, "BLOCK"))["contexts"],
                         "### Other changes in this pull request\n\nDIFF\n\nBLOCK")


class TagParserTests(unittest.TestCase):
    def test_reply_in_the_papers_tags_is_read_note_by_note(self):
        reply = tagged(("+b = warning_once(d)", "right", "G1 dict is unhashable"), ("-b = 2", "left", "the old value was fine"),
                       ("+ no such line", "right", "quotes something that is not there"))
        parsed = parse_tagged_review(reply, path="p.py", diff=HUNK_P)
        self.assertEqual((parsed["status"], parsed["overall"], parsed["invalid_findings"]), ("ok", {"ended": True}, 0))
        self.assertEqual([(f["note"], f["path"], f["from_line"], f["to_line"], f["side"], f["located"]) for f in parsed["findings"]], [
            ("G1 dict is unhashable", "p.py", 248, 248, "right", True),
            ("the old value was fine", "p.py", 248, 248, "left", True),
            ("quotes something that is not there", "p.py", None, None, "right", False)])
        self.assertEqual((parsed["findings"][0]["snippet"], parsed["findings"][0]["priority"]), ("+b = warning_once(d)", None))
        # Without a path (a whole-PR diff) the file comes from where the snippet is found.
        both = HUNK_P + "\n" + HUNK_W
        self.assertEqual(parse_tagged_review(tagged(("+new long warning string", "right", "too long")), diff=both)["findings"][0]["path"], "w.py")

    def test_no_findings_missing_tags_and_loose_formatting(self):
        self.assertEqual(parse_tagged_review("<end />")["findings"], [])
        self.assertEqual(parse_tagged_review("<think>fine</think>\n<end/>")["status"], "ok")
        self.assertEqual(parse_tagged_review("The change looks fine to me.")["status"], "malformed")  # the retry rule asks again
        self.assertEqual(parse_tagged_review("  ")["status"], "empty_output")
        cut_off = parse_tagged_review(tagged(("+c = 3", "right", "magic number"), end=False), path="p.py", diff=HUNK_P)
        self.assertEqual((cut_off["status"], cut_off["overall"], len(cut_off["findings"])), ("ok", {"ended": False}, 1))
        # Two comments without the separator, a comment without a quote, an empty note, a quote without a comment.
        loose = ("<diff>+c = 3</diff><side>right</side><note>one</note>\n<diff>-b = 2</diff><side>LEFT</side><note>two</note>\n"
                 "<note>three, about the hunk as a whole</note>\n<note>  </note>\n<diff>+c = 3</diff><side>right</side>\n<end />")
        parsed = parse_tagged_review(loose, path="p.py", diff=HUNK_P)
        self.assertEqual([(f["note"], f["side"], f["from_line"]) for f in parsed["findings"]],
                         [("one", "right", 249), ("two", "left", 248), ("three, about the hunk as a whole", "right", None)])
        self.assertEqual(parsed["invalid_findings"], 2)

    def test_quoted_snippets_are_found_in_the_diff(self):
        self.assertEqual(locate_snippet(HUNK_P, "+b = warning_once(d)\n+c = 3"), ("p.py", 248, 249))
        self.assertEqual(locate_snippet(HUNK_P, "b = warning_once(d)"), ("p.py", 248, 248))          # markers are optional
        self.assertEqual(locate_snippet(HUNK_P, "+ b = warning_once(d)\n+ c = 3"), ("p.py", 248, 249))  # the example's "+ " style
        self.assertEqual(locate_snippet(HUNK_P, " a = 1\n+b = warning_once(d)"), ("p.py", 247, 248))  # the removed line is skipped
        self.assertEqual(locate_snippet(HUNK_P, "-b = 2", "left"), ("p.py", 248, 248))
        self.assertEqual(locate_snippet(HUNK_P, "+c = 3\n+made up"), ("p.py", 249, 249))              # falls back to the first line
        self.assertIsNone(locate_snippet(HUNK_P, "nothing like it"))
        self.assertIsNone(locate_snippet(HUNK_P, "  \n"))


class HunkRunTests(unittest.TestCase):
    """A per-hunk run: one cell per (diff hunk, level), judged against the PR's references."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.contexts = self.root / "context_levels.jsonl"
        append_record(self.contexts, {
            "id": "o__r__1", "index": 0, "status": "ok", "language": "Python", "known_paths": ["p.py", "w.py"],
            "hunks": [
                {"id": "o__r__1__h0001", "index": 1, "file": "p.py", "new_start": 247, "new_count": 4,
                 "old_start": 247, "old_count": 3,
                 "levels": {"H01_min": hunk_level(HUNK_P),
                            "H02_function": hunk_level(HUNK_P, "H01_min", same=True)}},  # stored without a second copy
                {"id": "o__r__1__h0002", "index": 2, "file": "w.py", "new_start": 717, "new_count": 3,
                 "old_start": 717, "old_count": 3,
                 "levels": {"H01_min": hunk_level(HUNK_W),
                            "H02_function": hunk_level(HUNK_W, "H01_min", context="### changed: w.f\nBODY", tokens=20)}}]})
        self.dataset = self.root / "positive_samples.json"
        self.dataset.write_text(json.dumps([{"githubPrUrl": "https://github.com/o/r/pull/1",
                                             "project_main_language": "Python", "comments": REFS}]), encoding="utf-8")
        self.calls = []

        def reviewer(messages, max_tokens=None):
            text = messages[0]["content"]
            self.calls.append(text)
            if "a/p.py" in text:  # the second comment quotes a line that is not in the hunk
                reply = tagged(("+b = warning_once(d)", "right", "G1 dict passed to cached warning_once"),
                               ("+ something else", "right", "G2 same unhashable problem"))
            elif "## Relevant Code Context" in text:
                reply = "<end />"
            else:
                reply = tagged(("+new long warning string", "right", "G4 split the long warning string"))
            return {"text": reply, "finish_reason": "stop", "prompt_tokens": 10, "completion_tokens": 5, "seconds": 0.1}
        reviewer.identity = {"kind": "fake"}
        self.reviewer = reviewer

        def judge(prompt):  # G1 and G2 both match p.py's third reference; G4 matches w.py's first
            return "[3]" if "G1" in prompt or "G2" in prompt else "[1]"
        judge.identity = {"kind": "fixed"}
        self.judge = judge

    def tearDown(self):
        self._tmp.cleanup()

    def test_cells_reviews_judgments_and_both_tables(self):
        cells = discover_cells(self.contexts)
        self.assertEqual([(c.entry, c.level, c.pr, c.file, c.span) for c in cells], [
            ("o__r__1__h0001", "H01_min", "o__r__1", "p.py", (247, 250, 247, 249)),
            ("o__r__1__h0001", "H02_function", "o__r__1", "p.py", (247, 250, 247, 249)),
            ("o__r__1__h0002", "H01_min", "o__r__1", "w.py", (717, 719, 717, 719)),
            ("o__r__1__h0002", "H02_function", "o__r__1", "w.py", (717, 719, 717, 719))])
        self.assertEqual(cells[1].text, cells[0].text)  # the parent's text, filled in
        self.assertEqual(cells[0].parts, ReviewInput("Fix it", "Why it is needed.", HUNK_P, ""))
        self.assertEqual(cells[3].parts.contexts, "### changed: w.f\nBODY")
        self.assertEqual({c.group for c in cells}, {"o__r__1"})
        self.assertEqual(len(discover_cells(self.contexts, entries=["o__r__1"])), 4)  # by PR
        self.assertEqual([c.entry for c in discover_cells(self.contexts, entries=["o__r__1__h0002"], levels=["H01_min"])],
                         ["o__r__1__h0002"])  # or by hunk

        out = self.root / "out"
        prompt = load_review_prompt("aacr-hunk")
        reviews = run_reviews(cells, self.reviewer, prompt, out)
        self.assertEqual(len(self.calls), 3)  # hunk 1's two levels have the same input
        self.assertTrue(self.calls[0].startswith("You are an expert code reviewer. Your task is to review"))
        self.assertIn(f"**Title:** Fix it\n\n**Description:** Why it is needed.\n\n## Code Changes to Review\n\n```diff\n{HUNK_P}\n```", self.calls[0])
        self.assertNotIn("Relevant Code Context", self.calls[0])                              # Figure 16
        self.assertIn("## Relevant Code Context\n\n### changed: w.f\nBODY\n\n## Instructions", self.calls[2])  # Figure 17
        self.assertEqual([(r["status"], r.get("reused_from")) for r in reviews],
                         [("ok", None), ("ok", "H01_min"), ("ok", None), ("ok", None)])
        first = reviews[0]
        self.assertEqual((first["pr"], first["file"], first["prompt_version"], first["parser"]),
                         ("o__r__1", "p.py", AACR_PROMPT_VERSION, "aacr-tags-v1"))
        self.assertEqual([(f["path"], f["from_line"], f["located"]) for f in first["parsed"]["findings"]],
                         [("p.py", 248, True), ("p.py", None, False)])
        self.assertTrue((out / "reviews" / "o__r__1__h0002" / "H02_function.json").is_file())
        run_reviews(cells, self.reviewer, prompt, out)
        self.assertEqual(len(self.calls), 3)  # a second run reuses every saved review
        # The same cells under the Codex prompt are other reviews.
        self.assertEqual(plan_reviews(cells, self.reviewer, "RUBRIC", out)["send"]["cells"], 3)

        references = references_by_entry(self.dataset, cells)
        self.assertIs(references["o__r__1__h0002"], references["o__r__1"])
        judgments = run_judgments(reviews, references, self.judge, out)
        self.assertEqual([(j["entry"], j["level"], j["generated"], j["covered_indices"]) for j in judgments], [
            ("o__r__1__h0001", "H01_min", 2, [7]), ("o__r__1__h0001", "H02_function", 2, [7]),
            ("o__r__1__h0002", "H01_min", 1, [2]), ("o__r__1__h0002", "H02_function", 0, [])])

        rows, hunk_rows, coverage = build_hunk_tables(cells, reviews, judgments, references)
        self.assertEqual([(r["entry"], r["level"]) for r in rows], [("o__r__1", "H01_min"), ("o__r__1", "H02_function")])
        low, high = rows
        # H01: three findings in two hunks; G2 repeats G1's reference, so two count as matched.
        self.assertEqual((low["hunks"], low["hunks_reviewed"], low["hunks_ok"], low["review_status"]), (2, 2, 2, "ok"))
        self.assertEqual((low["input_tokens"], low["findings"], low["generated"], low["matched_generated"], low["covered"],
                          low["expected"]), (20, 3, 3, 2, 2, 8))
        self.assertEqual((low["recall"], low["precision"], low["human_covered"], low["human_expected"]), (0.25, 0.6667, 1, 1))
        self.assertEqual((high["generated"], high["covered"], high["input_tokens"]), (2, 1, 30))
        self.assertEqual(len(coverage), 16)
        self.assertEqual([c["reference"] for c in coverage if c["level"] == "H01_min" and c["covered"]], [2, 7])

        self.assertEqual(len(hunk_rows), 4)
        one = hunk_rows[0]
        self.assertEqual((one["entry"], one["pr"], one["file"], one["new_start"], one["new_end"]),
                         ("o__r__1__h0001", "o__r__1", "p.py", 247, 250))
        # Two of p.py's three references sit on lines 247-250; the review covers one of them.
        self.assertEqual((one["findings"], one["findings_matched"], one["references_on_hunk"],
                          one["references_on_hunk_covered"], one["references_covered"]), (2, 2, 2, 1, 1))
        self.assertEqual((hunk_rows[2]["references_on_hunk"], hunk_rows[2]["references_on_hunk_covered"]), (2, 1))
        self.assertEqual(hunk_rows[3]["findings"], 0)

        write_outputs(out, rows, coverage, hunk_rows)
        self.assertTrue(all((out / name).is_file() for name in ("table.csv", "table.json", "coverage.csv", "hunk_table.csv")))
        self.assertEqual(json.loads((out / "table.json").read_text(encoding="utf-8")), rows)
        summary = summarize_by_level(rows)
        self.assertEqual([(s["level"], s["recall_pooled"], s["statuses"]) for s in summary],
                         [("H01_min", 0.25, {"ok": 1}), ("H02_function", 0.125, {"ok": 1})])

    def test_a_reply_without_tags_gets_the_follow_up_that_asks_for_them(self):
        cells = discover_cells(self.contexts, entries=["o__r__1__h0002"], levels=["H01_min"])
        prompt = load_review_prompt("aacr-hunk")
        replies = iter(["I looked at the change and it seems fine.", "<end />"])
        seen = []

        def reviewer(messages, max_tokens=None):
            seen.append(messages)
            return {"text": next(replies), "finish_reason": "stop", "prompt_tokens": 10, "completion_tokens": 5, "seconds": 0.1}
        reviewer.identity, reviewer.max_tokens = {"kind": "fake"}, 4096
        policy = RetryPolicy.from_spec({"retry": {"follow_up": True}}, prompt.follow_up)
        record = run_reviews(cells, reviewer, prompt, self.root / "out", retry=policy)[0]
        self.assertEqual((record["status"], record["parsed"]["findings"], retry_rule.steps_taken(record)), ("ok", [], "follow_up"))
        self.assertEqual(record["retry"]["first"]["status"], "malformed")
        self.assertEqual([m["role"] for m in seen[1]], ["user", "assistant", "user"])
        self.assertEqual(seen[1][-1]["content"], AACR_FOLLOW_UP_REQUEST)
        self.assertIn("<end />", AACR_FOLLOW_UP_REQUEST)
        self.assertNotIn("JSON", AACR_FOLLOW_UP_REQUEST)

    def test_a_pr_with_an_unreviewed_hunk_is_marked_incomplete(self):
        cells = discover_cells(self.contexts, levels=["H01_min"])
        out = self.root / "out"
        reviews = run_reviews(cells, self.reviewer, load_review_prompt("aacr-hunk"), out)
        references = references_by_entry(self.dataset, cells)
        judgments = run_judgments(reviews, references, self.judge, out)
        rows, hunk_rows, _ = build_hunk_tables(cells, reviews[:1], judgments[:1], references)
        self.assertEqual((rows[0]["review_status"], rows[0]["hunks"], rows[0]["hunks_reviewed"], rows[0]["hunks_ok"]),
                         ("incomplete", 2, 1, 1))
        self.assertEqual(json.loads(rows[0]["hunk_statuses"]), {"missing": 1, "ok": 1})
        self.assertEqual((hunk_rows[1]["review_status"], hunk_rows[1]["findings"]), ("missing", 0))
        self.assertNotIn("findings_matched", hunk_rows[1])
        with self.assertRaisesRegex(ValueError, "per-hunk"):  # per-PR cells belong to build_table
            from src.review_outcomes.table import Cell
            build_hunk_tables([Cell("o__r__1", "L01_min", "text", 1)], [], [], references)

    def test_scripts_choose_per_hunk_cells_by_pr_and_read_the_profile(self):
        import importlib.util
        import sys

        from src.review_outcomes.profiles import read_notebook_profile

        path = Path(__file__).resolve().parents[1] / "scripts" / "outcome" / "run_reviews.py"
        spec = importlib.util.spec_from_file_location("run_reviews", path)
        script = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = script
        spec.loader.exec_module(script)
        cells = discover_cells(self.contexts)
        self.assertEqual(len(script.select_cells(cells, 1, None)), 4)        # the first PR: all its hunks
        self.assertEqual(len(script.select_cells(cells, None, ["o__r__1"])), 4)
        with self.assertRaises(SystemExit):
            script.select_cells(cells, None, ["o__r__1__h0001"])               # runs are chosen by PR
        profile = read_notebook_profile("cloud-hunk")
        self.assertEqual((profile["prompt"], profile["contexts"]), ("aacr-hunk", "data/aacr_hunk/context_levels.jsonl"))
        self.assertEqual(load_review_prompt(profile["prompt"]).reply_format, "aacr-tags")
        self.assertNotIn("prompt", read_notebook_profile("cloud"))  # the per-PR run keeps the Codex prompt
        self.assertEqual(profile["reviewer"], read_notebook_profile("cloud")["reviewer"])
        self.assertEqual(profile["judge"], read_notebook_profile("cloud")["judge"])


if __name__ == "__main__":
    unittest.main()
