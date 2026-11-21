"""Labels for reference comments that come without any (src/review_outcomes/reference_labels.py)."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.review_outcomes import reference_labels as labels  # noqa: E402
from src.review_outcomes.profiles import dataset_path, read_notebook_profile  # noqa: E402
from src.review_outcomes.table import Cell  # noqa: E402

VALID = {"valid": "yes", "kind": "missing tests", "finding": "The new function {x} has no tests.", "category": "maintainability and readability",
         "context": "Repo Level", "tier": "should fix", "reason": "r", "confidence": 0.7}
NOT_VALID = {"valid": "no", "kind": "question", "finding": "", "category": "none", "context": "none", "tier": "none"}


class FakeModel:
    identity = {"kind": "fake", "model": "m"}

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []

    def __call__(self, messages, max_tokens=None):
        self.calls.append(messages[0]["content"])
        return {"text": self.replies.pop(0), "completion_tokens": 5, "seconds": 0.1}


class ParseTests(unittest.TestCase):
    def test_reads_the_last_object_with_braces_in_strings_and_thinking_around_it(self):
        text = f"<think>{json.dumps(NOT_VALID)}</think> Here it is:\n```json\n{json.dumps(VALID)}\n```"
        got = labels.parse_labels(text)
        self.assertEqual((got["valid"], got["category"], got["context"], got["tier"]),
                         (True, "Maintainability and Readability", "Repo Level", "should_fix"))
        self.assertEqual((got["finding"], got["kind"], got["confidence"]), ("The new function {x} has no tests.", "missing tests", 0.7))

    def test_a_comment_that_is_not_valid_has_no_category_context_or_tier(self):
        got = labels.parse_labels(json.dumps(NOT_VALID))
        self.assertEqual((got["valid"], got["category"], got["context"], got["tier"], got["finding"]), (False, None, None, None, ""))

    def test_an_answer_outside_the_choices_is_no_answer(self):
        for change in ({"valid": "maybe"}, {"category": "Style"}, {"context": "Line Level"}, {"tier": "critical"}, {"tier": "none"}):
            self.assertIsNone(labels.parse_labels(json.dumps({**VALID, **change})), change)
        self.assertIsNone(labels.parse_labels("valid: yes"))


class LabelTests(unittest.TestCase):
    def setUp(self):
        self.cells = [Cell("o__r__1__h0001", "H03_file", "CODE-A", 10, pr="o__r__1", file="a.py", span=(10, 20, 10, 18))]
        comment = {"note": "Make sure to initialise p2", "path": "a.py", "side": "right", "from_line": 12, "to_line": 12,
                   "replies": ["p2 is initialised above", "I'm blind"], "thread": "o__r__1::7"}
        self.references = {"o__r__1": {"comments": [comment, {**comment, "note": "", "thread": "o__r__1::8"},
                                                    {**comment, "note": "Typo", "replies": [], "thread": "o__r__1::9"}]}}
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / "labels" / "reference_labels_m.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_prompt_shows_the_code_the_comment_and_its_replies(self):
        items = labels.reference_items(self.references, self.cells, FakeModel.identity)
        self.assertEqual([(i["reference"], i["thread"], i["code_from"]) for i in items],
                         [(0, "o__r__1::7", "o__r__1__h0001"), (2, "o__r__1::9", "o__r__1__h0001")])  # the empty comment is skipped
        prompt = items[0]["prompt"]
        for wanted in ("CODE-A", "Make sure to initialise p2", "Reply 1: p2 is initialised above", "Reply 2: I'm blind",
                       "Lines: 12-12 (right side)", '"Security Vulnerability"', "Repo Level", "blocking"):
            self.assertIn(wanted, prompt)
        self.assertIn("(no replies)", items[1]["prompt"])
        self.assertNotEqual(items[0]["key"], labels.reference_items(self.references, self.cells, {"model": "other"})[0]["key"])

    def test_labels_are_saved_kept_and_never_guessed(self):
        model = FakeModel([json.dumps(NOT_VALID), "no idea"])
        got = labels.label_references(self.references, self.cells, model, self.out, labeller="m")
        self.assertEqual((got[("o__r__1", 0)]["valid"], got[("o__r__1", 0)]["thread"], got[("o__r__1", 0)]["labeller"]), (False, "o__r__1::7", "m"))
        self.assertEqual(got[("o__r__1", 2)]["error"], "unreadable reply")
        self.assertNotIn("valid", got[("o__r__1", 2)])
        self.assertNotIn("prompt", json.loads(self.out.read_text(encoding="utf-8").splitlines()[0]))
        again = FakeModel([json.dumps(VALID)])  # only the unreadable one is asked again
        got = labels.label_references(self.references, self.cells, again, self.out, labeller="m")
        self.assertEqual(len(again.calls), 1)
        self.assertEqual((got[("o__r__1", 2)]["valid"], got[("o__r__1", 2)]["tier"], got[("o__r__1", 0)]["valid"]), (True, "should_fix", False))


class ReferenceFileTests(unittest.TestCase):
    def test_only_hunks_in_a_file_with_a_reference_are_kept(self):
        from src.review_outcomes import table
        with tempfile.TemporaryDirectory() as tmp:
            dataset = Path(tmp) / "positive_samples.json"
            dataset.write_text(json.dumps([
                {"githubPrUrl": "https://github.com/o/r/pull/1", "comments": [{"note": "x", "path": "src\\a.py"}, {"note": "", "path": "b.py"}]},
                {"githubPrUrl": "https://github.com/o/r/pull/2", "comments": []}]))
            cells = [Cell("o__r__1__h0001", "H01_min", "", 1, pr="o__r__1", file="src/a.py"),
                     Cell("o__r__1__h0002", "H01_min", "", 1, pr="o__r__1", file="b.py"),  # its only comment is empty
                     Cell("o__r__2__h0001", "H01_min", "", 1, pr="o__r__2", file="src/a.py"),  # another PR's file
                     Cell("o__r__3", "L01_min", "", 1)]  # a per-PR cell
            self.assertEqual([c.entry for c in table.in_reference_files(cells, dataset)], ["o__r__1__h0001", "o__r__3"])


class ProfileTests(unittest.TestCase):
    def test_the_pilot_reviews_only_hunks_in_reference_files(self):
        self.assertTrue(read_notebook_profile("cloud-ccrb-hunk")["only_reference_files"])
        self.assertNotIn("only_reference_files", read_notebook_profile("cloud-hunk"))

    def test_a_profile_names_its_own_reference_comments_or_uses_aacr_bench(self):
        self.assertEqual(dataset_path({}, Path("/p")), Path("/p/data/aacr/positive_samples.json"))
        self.assertEqual(dataset_path({"dataset": "data/x/positive_samples.json"}, Path("/p")), Path("/p/data/x/positive_samples.json"))
        pilot = read_notebook_profile("cloud-ccrb-hunk")
        self.assertEqual((pilot["dataset"], pilot["contexts"], pilot["levels"]),
                         ("data/ccrb_hunk/positive_samples.json", "data/ccrb_hunk/context_levels.jsonl",
                          ["H01_min", "H02_function", "H03_file", "H04_callees1"]))
        self.assertNotIn("dataset", read_notebook_profile("cloud-hunk"))


if __name__ == "__main__":
    unittest.main()
