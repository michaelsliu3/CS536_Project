import json
import tempfile
import unittest
from pathlib import Path

from src.review_outcomes.judge import parse_judge_answer, score_review
from src.review_outcomes.parse import normalize_path, parse_review
from src.review_outcomes.prompts import (
    REVIEW_REQUEST, RUBRIC_PATH, judge_prompt, load_rubric, prompt_sha256, review_messages,
)
from src.review_outcomes.table import (
    build_table, discover_cells, run_judgments, run_reviews, summarize_by_level,
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
        summary = summarize_by_level(rows)
        self.assertEqual([s["level"] for s in summary], ["L01_min", "L02_function"])
        self.assertAlmostEqual(summary[0]["recall_pooled"], 0.125)
        self.assertEqual(summary[1]["statuses"], {"too_long": 1})

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


if __name__ == "__main__":
    unittest.main()
