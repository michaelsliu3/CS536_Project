"""Unit tests for the Codex blocks report's helpers (no saved run needed)."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "analysis"))

import codex_blocks_report as report  # noqa: E402


class Helpers(unittest.TestCase):
    def test_a_hunk_names_its_repository(self):
        self.assertEqual(report.repository("owner__repo__123__h0004"), "owner__repo")

    def test_only_leading_blocks_with_every_session_saved_are_taken(self):
        with tempfile.TemporaryDirectory() as folder:
            sessions = Path(folder)
            order = {"block": 2, "hunks": ["a__b__1__h0001", "a__b__1__h0002", "a__b__2__h0001", "a__b__2__h0002", "c__d__3__h0001", "c__d__3__h0002"]}
            for hunk in order["hunks"][:3] + order["hunks"][4:]:           # the second block lacks one session
                (sessions / hunk).mkdir()
                (sessions / hunk / "run.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
            self.assertEqual(report.finished_hunks(order, sessions), (order["hunks"][:2], 1))   # the third block is not counted either
            self.assertEqual(report.finished_hunks(order, sessions, blocks=0), ([], 0))

    def test_cached_input_is_cheaper_than_new_input(self):
        self.assertAlmostEqual(report.cost(1_000_000, 0), 2.00)
        self.assertAlmostEqual(report.cost(1_000_000, 100_000, cached=900_000, written=50_000), 0.05 * 2.00 + 0.05 * 2.50 + 0.9 * 0.10 + 1.00)

    def test_a_hunks_size_group(self):
        self.assertEqual(report.size_group(2, (2, 6)), "1. up to 2 changed lines")
        self.assertEqual(report.size_group(6, (2, 6)), "2. 3 to 6 changed lines")
        self.assertEqual(report.size_group(7, (2, 6)), "3. over 6 changed lines")


class GroupedDifference(unittest.TestCase):
    def test_sums_and_the_difference_per_group(self):
        group = ["x", "x", "y", "y", "y"]
        unit = ["r1", "r2", "r1", "r2", "r3"]
        out = report.grouped_difference(group, unit, [1, 0, 1, 1, 0], [1, 1, 0, 0, 0], draws=200)
        self.assertEqual((out["x"]["items"], out["x"]["first"], out["x"]["second"], out["x"]["difference"][0]), (2, 1.0, 2.0, -1.0))
        self.assertEqual((out["y"]["items"], out["y"]["first"], out["y"]["second"], out["y"]["difference"][0]), (3, 2.0, 0.0, 2.0))

    def test_the_interval_draws_whole_units(self):
        # every unit has the same difference of +1, so any draw of three units sums to +3
        out = report.grouped_difference(["g"] * 6, ["a", "a", "b", "b", "c", "c"], [1, 1, 1, 1, 1, 1], [1, 0, 1, 0, 1, 0], draws=300)
        self.assertEqual(out["g"]["difference"], [3.0, 3.0, 3.0])
        # one unit carries all of the difference: a draw has it 0 to 3 times
        out = report.grouped_difference(["g"] * 3, ["a", "b", "c"], [3, 0, 0], [0, 0, 0], draws=2000)
        low, high = out["g"]["difference"][1:]
        self.assertEqual(out["g"]["difference"][0], 3.0)
        self.assertLessEqual(low, 3.0)
        self.assertGreaterEqual(high, 3.0)
        self.assertEqual(low % 3, 0)


if __name__ == "__main__":
    unittest.main()
