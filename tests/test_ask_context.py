"""Asking the reviewer whether it needs more code (src/review_outcomes/ask.py)."""
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.review_outcomes import ask  # noqa: E402
from src.review_outcomes.table import Cell  # noqa: E402


class FakePrompt:
    def messages(self, text, parts=None):
        return [{"role": "user", "content": f"REVIEW THIS: {text}"}]


class FakeModel:
    identity = {"kind": "fake", "model": "m"}

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def __call__(self, messages, max_tokens=None):
        self.calls.append(messages)
        for needle, reply in self.answers:
            if needle in messages[0]["content"]:
                return {"text": reply, "prompt_tokens": 50, "completion_tokens": 9, "seconds": 0.1}
        return {"text": "I would rather not say.", "prompt_tokens": 50, "completion_tokens": 9, "seconds": 0.1}


class ParseAskTests(unittest.TestCase):
    def test_reads_the_answer_and_ignores_thinking_and_fences(self):
        text = '<think>{"need_file": "no"}</think>\n```json\n{"need_file": "Yes", "likelihood": 4, "missing": " the definition of get "}\n```'
        self.assertEqual(ask.parse_ask(text), {"need_file": True, "likelihood": 4, "missing": "the definition of get"})
        self.assertEqual(ask.parse_ask('{"need_file": false, "likelihood": "2"}'), {"need_file": False, "likelihood": 2, "missing": ""})

    def test_a_likelihood_outside_one_to_five_is_dropped_not_guessed(self):
        self.assertIsNone(ask.parse_ask('{"need_file": "no", "likelihood": 9}')["likelihood"])
        self.assertIsNone(ask.parse_ask('{"need_file": "no", "likelihood": true}')["likelihood"])
        self.assertIsNone(ask.parse_ask('{"need_file": "no"}')["likelihood"])

    def test_no_yes_or_no_is_no_answer(self):
        for text in ('{"need_file": "maybe"}', "yes, I need the file", '{"likelihood": 5}', ""):
            self.assertIsNone(ask.parse_ask(text))


class AskTests(unittest.TestCase):
    def setUp(self):
        self.cells = [Cell(f"o__r__1__h000{i}", "H01_min", f"DIFF{i}", 10, pr="o__r__1", file="a.py", span=(1, 2, 1, 2))
                      for i in (1, 2, 3, 4)]
        self.reviews = {"o__r__1__h0001": {"status": "ok", "key": "k1", "reply": {"text": "REVIEW1"}},
                        "o__r__1__h0002": {"status": "ok", "key": "k2", "reply": {"text": "REVIEW2"}},
                        "o__r__1__h0003": {"status": "error", "error": "timeout"},
                        "o__r__1__h0004": None}
        self._tmp = tempfile.TemporaryDirectory()
        self.out = ask.asks_path(Path(self._tmp.name), "H01_min", "cost")

    def tearDown(self):
        self._tmp.cleanup()

    def items(self, variant="cost", identity=FakeModel.identity):
        return ask.ask_items(self.cells, self.reviews, FakePrompt(), identity, variant)

    def test_the_question_follows_the_saved_review(self):
        items = self.items()
        self.assertEqual([i["entry"] for i in items], ["o__r__1__h0001", "o__r__1__h0002"])  # only finished reviews
        messages = items[0]["messages"]
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user"])
        self.assertEqual((messages[0]["content"], messages[1]["content"]), ("REVIEW THIS: DIFF1", "REVIEW1"))
        self.assertIn("is not free", messages[2]["content"])
        self.assertNotIn("is not free", self.items("plain")[0]["messages"][2]["content"])
        self.assertNotEqual(items[0]["key"], self.items("plain")[0]["key"])
        self.assertNotEqual(items[0]["key"], self.items(identity={"kind": "fake", "model": "other"})[0]["key"])

    def test_answers_are_saved_kept_and_never_guessed(self):
        model = FakeModel([("DIFF1", '{"need_file": "yes", "likelihood": 5, "missing": "callers"}')])
        asks = ask.run_asks(self.items(), model, self.out, concurrency=2)
        self.assertEqual((asks["o__r__1__h0001"]["need_file"], asks["o__r__1__h0001"]["likelihood"]), (True, 5))
        self.assertEqual(asks["o__r__1__h0002"]["error"], "unreadable reply")
        self.assertNotIn("need_file", asks["o__r__1__h0002"])
        self.assertNotIn("messages", asks["o__r__1__h0001"])
        self.assertEqual(ask.load_asks(self.out).keys(), asks.keys())
        # A second run asks only what failed.
        again = FakeModel([("DIFF2", '{"need_file": "no", "likelihood": 1, "missing": ""}')])
        asks = ask.run_asks(self.items(), again, self.out)
        self.assertEqual(len(again.calls), 1)
        self.assertEqual((asks["o__r__1__h0002"]["need_file"], asks["o__r__1__h0001"]["need_file"]), (False, True))
        # A changed review is asked about again.
        self.reviews["o__r__1__h0001"]["reply"]["text"] = "ANOTHER REVIEW"
        third = FakeModel([])
        ask.run_asks(self.items(), third, self.out)
        self.assertEqual(len(third.calls), 1)


if __name__ == "__main__":
    unittest.main()
