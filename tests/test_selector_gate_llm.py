"""Tests for scripts/analysis/selector_gate_llm.py: the question, the text the model reads and the
reading of its answer. No model is loaded; a tokenizer of whole words stands in."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
spec = importlib.util.spec_from_file_location("selector_gate_llm", PROJECT_ROOT / "scripts" / "analysis" / "selector_gate_llm.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)


class Words:
    """A tokenizer whose tokens are the words of the text."""

    class Encoded:
        def __init__(self, ids):
            self.input_ids = ids

    def __call__(self, text, add_special_tokens=False):
        return self.Encoded(text.split())

    def decode(self, ids):
        return " ".join(ids)


class Question(unittest.TestCase):
    def test_each_level_names_what_was_seen_and_what_would_be_added(self):
        first, last = gate.question(0), gate.question(2)
        self.assertIn("only the diff and the pull request description", first)
        self.assertIn("more code: the function the hunk changes", first)
        self.assertIn("the diff and the whole file", last)
        self.assertIn("more code: the definitions of what the hunk calls", last)
        for level in range(3):
            self.assertTrue(gate.question(level).endswith("Answer with Yes or No only."))
            self.assertIn("costs a full extra request", gate.question(level))


class Text(unittest.TestCase):
    hunk = "## Pull request\n\nTitle: t\n\none two three four\n\n## Diff\n\na b c d e f"

    def test_the_diff_comes_first_and_keeps_most_of_the_room(self):
        self.assertEqual(gate.split_hunk_text(self.hunk)[1], "## Diff\n\na b c d e f")
        whole = gate.cut_hunk_text(Words(), self.hunk, 100)
        self.assertTrue(whole.startswith("## Diff a b c d e f"))
        self.assertTrue(whole.endswith("one two three four"))
        short = gate.cut_hunk_text(Words(), self.hunk, 10)                     # 8 diff words and 8 pull request words do not fit
        self.assertEqual(short, "## Diff a b c d\n\n## Pull request Title:")       # the diff keeps 6 of the 10

    def test_the_findings_are_numbered_and_cut(self):
        self.assertEqual(gate.findings_text(Words(), ["First  note.", "Second\nnote."], 50), "1. First note.\n2. Second note.")
        self.assertEqual(gate.findings_text(Words(), [], 50), "(The review has no findings.)")
        self.assertEqual(gate.findings_text(Words(), ["a b c d e f"], 4), "1. a b c …")

    def test_the_message_holds_the_question_the_hunk_and_the_findings(self):
        message = gate.gate_message(Words(), 1, self.hunk, ["Check the caller."], 100, 50)
        self.assertTrue(message.startswith(gate.question(1)))
        self.assertLess(message.index("## Diff"), message.index("## The first review's findings"))
        self.assertTrue(message.endswith("1. Check the caller."))


class Answer(unittest.TestCase):
    def test_the_score_is_log_yes_minus_log_no_over_the_spellings(self):
        rows = np.log(np.array([[0.4, 0.2, 0.1, 0.1, 0.2], [0.05, 0.05, 0.4, 0.4, 0.1]]))
        score = gate.yes_minus_no(rows, yes=[0, 1], no=[2, 3])
        self.assertAlmostEqual(score[0], np.log(0.6 / 0.2))
        self.assertAlmostEqual(score[1], np.log(0.1 / 0.8))


class Reading(unittest.TestCase):
    def test_hunks_come_in_the_input_files_order_with_the_notes_of_each_level(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "hunk_outcomes.json").write_text(json.dumps({"rows": [{"hunk": "o__r__1__h0002"}, {"hunk": "o__r__1__h0001"}]}), encoding="utf-8")
            entry = {"hunks": [{"id": name, "levels": {"H01_min": {"text": f"text of {name}"}}} for name in ("o__r__1__h0001", "o__r__1__h0002")]}
            (root / "context_levels_slim.jsonl").write_text(json.dumps(entry) + "\n", encoding="utf-8")
            for name in ("o__r__1__h0001", "o__r__1__h0002"):
                for level in gate.LEVELS:
                    path = root / "run" / "reviews" / name / f"{level}.json"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(json.dumps({"parsed": {"findings": [{"note": f"{level} of {name}"}]}}), encoding="utf-8")
            hunks = gate.load_hunks(root, root / "run")
            self.assertEqual([h["hunk"] for h in hunks], ["o__r__1__h0002", "o__r__1__h0001"])
            self.assertEqual(hunks[0]["text"], "text of o__r__1__h0002")
            self.assertEqual(hunks[0]["notes"], [["H01_min of o__r__1__h0002"], ["H02_function of o__r__1__h0002"], ["H03_file of o__r__1__h0002"]])
            self.assertEqual(len(gate.load_hunks(root, root / "run", limit=1)), 1)


if __name__ == "__main__":
    unittest.main()
