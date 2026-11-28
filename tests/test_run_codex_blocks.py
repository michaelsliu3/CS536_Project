"""The block-by-block Codex run in a random order (scripts/harness/run_codex_blocks.py): which
hunks it takes, the order and its file, the blocks, and how a saved session is read."""
import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

spec = importlib.util.spec_from_file_location("run_codex_blocks", PROJECT_ROOT / "scripts" / "harness" / "run_codex_blocks.py")
blocks = importlib.util.module_from_spec(spec)
sys.modules["run_codex_blocks"] = blocks
spec.loader.exec_module(blocks)

LEVELS = ["H01_min", "H02_function"]


class OrderTests(unittest.TestCase):
    def test_only_hunks_reviewed_at_every_level_are_taken(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp)
            with (run / "hunk_table.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["entry", "level", "review_status"])
                writer.writerows([["b__h0002", "H01_min", "ok"], ["b__h0002", "H02_function", "ok"],
                                  ["a__h0001", "H01_min", "ok"], ["a__h0001", "H02_function", "ok"],
                                  ["c__h0001", "H01_min", "too_long"], ["c__h0001", "H02_function", "too_long"],
                                  ["d__h0001", "H01_min", "ok"]])           # no review at the second level
            self.assertEqual(blocks.reviewed_hunks(run, LEVELS), ["a__h0001", "b__h0002"])

    def test_the_order_depends_on_the_seed_and_not_on_the_input_order(self):
        hunks = [f"o__r__1__h{i:04d}" for i in range(50)]
        first = blocks.random_order(hunks, 536)
        self.assertEqual(sorted(first), hunks)
        self.assertNotEqual(first, hunks)
        self.assertEqual(blocks.random_order(list(reversed(hunks)), 536), first)
        self.assertNotEqual(blocks.random_order(hunks, 537), first)

    def test_a_saved_order_is_read_back_and_must_hold_the_same_hunks(self):
        hunks = [f"o__r__1__h{i:04d}" for i in range(10)]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "harness" / "order.json"
            made = blocks.load_order(path, hunks, 536, 4)
            self.assertEqual((made["seed"], made["block"], made["hunks"]), (536, 4, blocks.random_order(hunks, 536)))
            again = blocks.load_order(path, list(reversed(hunks)), 999, 7)     # the file wins over new arguments
            self.assertEqual((again["seed"], again["block"], again["hunks"]), (536, 4, made["hunks"]))
            with self.assertRaises(SystemExit):
                blocks.load_order(path, hunks[:-1], 536, 4)

    def test_blocks_cover_the_order_once(self):
        order = list("abcdefghij")
        self.assertEqual(blocks.blocks_of(order, 4), [list("abcd"), list("efgh"), list("ij")])
        self.assertEqual(blocks.blocks_of(order, 10), [order])


class SessionTests(unittest.TestCase):
    def test_a_session_is_read_from_its_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            sessions = Path(tmp)
            for hunk, text in (("a", json.dumps({"status": "ok"})), ("b", json.dumps({"status": "timeout"})), ("c", "{not json")):
                (sessions / hunk).mkdir()
                (sessions / hunk / "run.json").write_text(text, encoding="utf-8")
            (sessions / "d").mkdir()                                            # started, no summary yet
            self.assertEqual([blocks.session_status(sessions, h) for h in "abcde"], ["ok", "timeout", "failed", "not run", "not run"])

    def test_a_server_that_does_not_answer_is_reported(self):
        self.assertFalse(blocks.server_answers("http://127.0.0.1:9"))


if __name__ == "__main__":
    unittest.main()
