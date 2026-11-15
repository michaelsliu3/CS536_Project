"""The severity labels and the review score (docs/review_score_design.md)."""
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.review_outcomes import score, severity  # noqa: E402
from src.review_outcomes.table import Cell  # noqa: E402


def cell(entry, level, file, span, text="CODE"):
    return Cell(entry, level, f"{text} of {entry}", 10, pr="o__r__1", file=file, span=span)


class FakeModel:
    """A chat backend that answers from a table of (text in the prompt, reply)."""

    identity = {"kind": "fake", "model": "m"}

    def __init__(self, answers):
        self.answers, self.prompts = answers, []

    def __call__(self, messages, max_tokens=None):
        prompt = messages[0]["content"]
        self.prompts.append(prompt)
        for needle, reply in self.answers:
            if needle in prompt:
                return {"text": reply, "completion_tokens": 5, "seconds": 0.1}
        return {"text": "I am not sure.", "completion_tokens": 5, "seconds": 0.1}


class ParseAnswerTests(unittest.TestCase):
    def test_reads_the_last_valid_object_and_ignores_thinking(self):
        fields = {"tier": severity.TIERS}
        text = '<think>{"tier": "nit"} maybe</think>\nHere:\n```json\n{"tier": "Blocking", "reason": "breaks the API", "confidence": 0.9}\n```'
        self.assertEqual(severity.parse_answer(text, fields), {"tier": "blocking", "reason": "breaks the API", "confidence": 0.9})
        self.assertEqual(severity.parse_answer('{"tier": "should fix"}', fields), {"tier": "should_fix"})
        self.assertEqual(severity.parse_answer('thinking...</think>{"tier": "nit"}', fields), {"tier": "nit"})

    def test_an_answer_outside_the_allowed_values_is_not_guessed(self):
        self.assertIsNone(severity.parse_answer('{"tier": "major"}', {"tier": severity.TIERS}))
        self.assertIsNone(severity.parse_answer("blocking, I think", {"tier": severity.TIERS}))
        self.assertIsNone(severity.parse_answer('{"reason": "no tier"}', {"tier": severity.TIERS}))
        self.assertIsNone(severity.parse_answer("", {"tier": severity.TIERS}))


class LabelReferencesTests(unittest.TestCase):
    def setUp(self):
        self.cells = [cell("o__r__1__h0001", "H03_file", "a.py", (10, 20, 10, 18)),
                      cell("o__r__1__h0002", "H03_file", "a.py", (40, 50, 38, 48)),
                      cell("o__r__1__h0003", "H03_file", "b.py", (1, 5, 1, 5))]
        self.references = {"o__r__1": {"comments": [
            {"note": "RACE on the shared map", "path": "a.py", "from_line": 44, "to_line": 45, "side": "right",
             "source_model": "SecretModel", "is_ai_comment": True},
            {"note": "rename tmp", "path": "b.py", "from_line": 90, "to_line": 90, "side": "right"},
            {"note": "", "path": "a.py", "from_line": 1, "to_line": 1},
            {"note": "no hunk here", "path": "c.py", "from_line": 3, "to_line": 3}]}}
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name) / "labels" / "severity_m.jsonl"

    def tearDown(self):
        self._tmp.cleanup()

    def test_the_comment_gets_the_code_of_its_own_hunk(self):
        comments = self.references["o__r__1"]["comments"]
        self.assertEqual(severity.code_for_reference(comments[0], self.cells), ("CODE of o__r__1__h0002", "o__r__1__h0002"))
        self.assertEqual(severity.code_for_reference(comments[1], self.cells)[1], "o__r__1__h0003")  # same file, no overlap
        self.assertIsNone(severity.code_for_reference(comments[3], self.cells)[1])

    def test_labels_are_saved_kept_and_made_without_the_author(self):
        model = FakeModel([("RACE", '{"tier": "blocking", "reason": "data race", "confidence": 0.8}'),
                           ("rename tmp", '{"tier": "nit", "reason": "a name"}')])
        labels = severity.label_references(self.references, self.cells, model, self.out, labeller="m")
        self.assertEqual({k: v.get("tier") for k, v in labels.items()},
                         {("o__r__1", 0): "blocking", ("o__r__1", 1): "nit", ("o__r__1", 3): None})
        self.assertEqual(labels[("o__r__1", 3)]["error"], "unreadable reply")  # never a guessed tier
        self.assertEqual(len(model.prompts), 3)  # the comment with no text is not labelled
        self.assertFalse(any("SecretModel" in p or "is_ai_comment" in p for p in model.prompts))
        self.assertIn("Lines: 44-45 (right side)", model.prompts[0])
        self.assertEqual((labels[("o__r__1", 0)]["rubric"], labels[("o__r__1", 0)]["labeller"]), (severity.RUBRIC_VERSION, "m"))
        self.assertEqual(severity.load_labels(self.out).keys(), labels.keys())
        # A second run sends only what failed; another model's labels are made again.
        again = FakeModel([("no hunk here", '{"tier": "should_fix"}')])
        labels = severity.label_references(self.references, self.cells, again, self.out)
        self.assertEqual((len(again.prompts), labels[("o__r__1", 3)]["tier"], labels[("o__r__1", 0)]["tier"]),
                         (1, "should_fix", "blocking"))
        other = FakeModel([])
        other.identity = {"kind": "fake", "model": "other"}
        severity.label_references(self.references, self.cells, other, self.out)
        self.assertEqual(len(other.prompts), 3)


class ScoreTests(unittest.TestCase):
    def coverage(self, covered):
        return [{"entry": "o__r__1", "level": "H01_min", "reference": i, "covered": i in covered,
                 "human": i == 2, "category": "Code Defect"} for i in range(3)]

    def setUp(self):
        self.labels = {("o__r__1", 0): {"tier": "blocking"}, ("o__r__1", 1): {"tier": "should_fix"},
                       ("o__r__1", 2): {"tier": "nit"}}

    def test_a_covered_reference_counts_by_its_severity(self):
        # One blocking (5), one should-fix (2), one nit (0.5); the blocking one and the nit are covered.
        row, = score.score_rows(self.coverage({0, 2}), self.labels)
        self.assertEqual((row["blocking_covered"], row["should_fix_covered"], row["nit_covered"]), (1, 0, 1))
        self.assertEqual((row["covered_weight"], row["reference_weight"], row["score"]), (5.5, 7.5, 0.7333))
        summary, = score.summarize([row])
        self.assertEqual((summary["blocking_covered"], summary["blocking_references"], summary["score"]), (1, 1, 0.7333))
        # Covering only the should-fix reference is worth less than covering only the blocking one.
        fix, = score.score_rows(self.coverage({1}), self.labels)
        block, = score.score_rows(self.coverage({0}), self.labels)
        self.assertEqual((fix["score"], block["score"]), (round(2 / 7.5, 4), round(5 / 7.5, 4)))

    def test_the_level_score_is_summed_over_prs_not_averaged(self):
        other = [{"entry": "o__r__2", "level": "H01_min", "reference": 0, "covered": False, "human": False, "category": "x"}]
        labels = {**self.labels, ("o__r__2", 0): {"tier": "blocking"}}
        rows = score.score_rows(self.coverage({0, 2}) + other, labels)
        self.assertEqual([r["score"] for r in rows], [0.7333, 0.0])
        self.assertEqual(score.summarize(rows)[0]["score"], round(5.5 / 12.5, 4))

    def test_weightings_multipliers_and_missing_labels(self):
        flat, = score.score_rows(self.coverage({0}), self.labels, score.Params(weights=score.WEIGHTINGS["flat"]))
        self.assertEqual(flat["score"], round(1 / 3, 4))  # the flat weighting is plain recall
        only, = score.score_rows(self.coverage({1, 2}), self.labels, score.Params(weights=score.WEIGHTINGS["blocking_only"]))
        self.assertEqual(only["score"], 0.0)
        boosted = score.Params(category_multiplier={"Code Defect": 2.0}, human_multiplier=1.5)
        row, = score.score_rows(self.coverage({2}), self.labels, boosted)
        self.assertEqual((row["reference_weight"], row["covered_weight"]), (10 + 4 + 1.5, 1.5))
        del self.labels[("o__r__1", 1)]
        self.labels[("o__r__1", 2)] = {"error": "unreadable reply"}
        row, = score.score_rows(self.coverage({0, 1}), self.labels)
        self.assertEqual((row["unlabelled"], row["reference_weight"], row["score"]), (2, 5.0, 1.0))


if __name__ == "__main__":
    unittest.main()
