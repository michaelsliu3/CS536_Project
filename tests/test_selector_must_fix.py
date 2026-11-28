"""Tests for scripts/analysis/selector_must_fix.py: reading the selectors' input file, the budget
rule, the statistics and the report, on a small hand-made set."""
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
spec = importlib.util.spec_from_file_location("selector_must_fix", PROJECT_ROOT / "scripts" / "analysis" / "selector_must_fix.py")
must_fix = importlib.util.module_from_spec(spec)
spec.loader.exec_module(must_fix)

LEVELS = must_fix.LEVELS


def hunk(name: str, pr: str, lines: int, tokens: list[int], covered: list[list[int]]) -> dict:
    return {"hunk": name, "pr": pr, "added": lines, "removed": 0,
            "levels": {level: {"tokens": tokens[l], "covered": covered[l]} for l, level in enumerate(LEVELS)}}


def fixture() -> dict:
    """Two repositories. o__a__1 has references 0 (blocking) and 1 (nit); o__b__2 has reference 0 (blocking).
    Hunk a1 covers the blocking comment only at the file level; hunk b1 covers its own at every level."""
    rows = [hunk("o__a__1__h0001", "o__a__1", 30, [10, 30, 50, 90], [[], [], [0], []]),
            hunk("o__a__1__h0002", "o__a__1", 2, [10, 12, 14, 16], [[1], [1], [1], [1]]),
            hunk("o__a__1__h0003", "o__a__1", 1, [10, 20, 30, 40], [[], [], [], []]),
            hunk("o__b__2__h0001", "o__b__2", 20, [10, 40, 60, 80], [[0], [0], [0], [0]]),
            hunk("o__b__2__h0002", "o__b__2", 1, [10, 18, 26, 34], [[], [], [], []])]
    refs = {"o__a__1": {"0": {"tier": "blocking"}, "1": {"tier": "nit"}}, "o__b__2": {"0": {"tier": "blocking"}}}
    return {"levels": LEVELS, "rows": rows, "refs": refs}


def loaded() -> tuple[dict, np.ndarray]:
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "hunk_outcomes.json"
        path.write_text(json.dumps(fixture()), encoding="utf-8")
        data = must_fix.load_outcomes(path)
    on = np.zeros((5, 3), dtype=bool)                             # which reference sits on which hunk's lines
    on[0, data["reference"][("o__a__1", 0)]] = True
    on[1, data["reference"][("o__a__1", 1)]] = True
    on[3, data["reference"][("o__b__2", 0)]] = True
    return data, on


class Reading(unittest.TestCase):
    def test_the_input_file_becomes_arrays(self):
        data, _ = loaded()
        self.assertEqual(data["cov"].shape, (5, 4, 3))
        self.assertEqual(data["tier"].tolist(), ["blocking", "nit", "blocking"])
        self.assertEqual(data["repo"].tolist(), ["o__a", "o__a", "o__a", "o__b", "o__b"])
        self.assertEqual(data["lines"].tolist(), [30, 2, 1, 20, 1])
        self.assertTrue(data["cov"][0, 2, data["reference"][("o__a__1", 0)]])
        self.assertFalse(data["cov"][0, 1, data["reference"][("o__a__1", 0)]])
        self.assertEqual(data["tok"][3].tolist(), [10, 40, 60, 80])


class Choices(unittest.TestCase):
    def test_hunks_go_up_in_order_until_the_budget_is_spent(self):
        tok = np.array([[10, 30], [10, 20], [10, 60], [10, 10]], dtype=float)     # extra tokens 20, 10, 50, 0
        order = np.array([2, 0, 1, 3])
        self.assertEqual(must_fix.upgrade_choice(tok, order, 1, 1.0).tolist(), [1, 1, 1, 1])
        self.assertEqual(must_fix.upgrade_choice(tok, order, 1, 50 / 80).tolist(), [0, 0, 1, 0])
        self.assertEqual(must_fix.upgrade_choice(tok, order, 1, 70 / 80).tolist(), [1, 0, 1, 0])
        self.assertEqual(must_fix.upgrade_choice(tok, order, 1, 0.5).tolist(), [0, 0, 0, 0])   # the first hunk alone costs more

    def test_the_highest_score_comes_first_and_ties_keep_their_order(self):
        self.assertEqual(must_fix.order_of([0.1, 0.9, 0.9, 0.5]).tolist(), [1, 2, 3, 0])

    def test_covered_counts_the_wanted_references_some_hunk_covers(self):
        data, _ = loaded()
        blocking = data["tier"] == "blocking"
        self.assertEqual(must_fix.covered(data["cov"], np.zeros(5, dtype=int), blocking), 1)
        self.assertEqual(must_fix.covered(data["cov"], np.array([2, 0, 0, 0, 0]), blocking), 2)
        self.assertEqual(must_fix.covered(data["cov"], np.full(5, 3), blocking), 1)


class Statistics(unittest.TestCase):
    def test_auc_is_the_share_of_pairs_ranked_right_with_ties_as_half(self):
        self.assertEqual(must_fix.auc([1, 0, 0], [3, 2, 1]), 1.0)
        self.assertEqual(must_fix.auc([1, 0, 0], [1, 2, 3]), 0.0)
        self.assertEqual(must_fix.auc([1, 0], [5, 5]), 0.5)
        self.assertNotEqual(must_fix.auc([0, 0], [1, 2]), must_fix.auc([0, 0], [1, 2]))       # nan: no hunk with y

    def test_fifths_are_equal_in_size_even_with_equal_values(self):
        self.assertEqual(must_fix.fifths([1] * 10).tolist(), [0, 0, 1, 1, 2, 2, 3, 3, 4, 4])
        self.assertEqual(must_fix.fifths([50, 10, 40, 20, 30]).tolist(), [4, 0, 3, 1, 2])

    def test_the_stratified_auc_compares_hunks_of_one_stratum_only(self):
        y, strata = [0, 1, 0, 1], [0, 0, 1, 1]
        score = [1, 2, 10, 11]                                    # right inside each stratum, wrong across them for one pair
        self.assertEqual(must_fix.stratified_auc(y, score, strata), 1.0)
        self.assertEqual(must_fix.auc(y, score), 0.75)

    def test_the_gap_in_shares_and_its_interval(self):
        flag, group = np.array([True, True, False, False]), np.array([True, False, False, False])
        self.assertAlmostEqual(must_fix.share_gap(flag, group), 1 - 1 / 3)
        repo = np.array(["a", "a", "b", "b"])
        low, high = must_fix.interval(repo, lambda i: must_fix.share_gap(flag, group, i), draws=200, seed=1)
        self.assertLessEqual(low, high)
        self.assertEqual(must_fix.interval(repo, lambda i: 0.25, draws=20), (0.25, 0.25))


class Report(unittest.TestCase):
    def setUp(self):
        self.data, self.on = loaded()
        # the model says Yes (above 0) for the two must-fix hunks and one other; the facts model ranks by size
        self.scores = {"zero-shot 7B": np.array([2.0, -1.0, 0.5, 1.0, -2.0]), "tabular, logistic": np.array([0.9, 0.2, 0.1, 0.8, 0.1])}
        self.report = must_fix.build_report(self.data, self.on, self.scores, spend=0.3, draws=50, random_orders=20)

    def test_counts(self):
        r = self.report
        self.assertEqual((r["hunks"], r["repositories"], r["references"], r["blocking_references"]), (5, 2, 3, 2))
        self.assertEqual((r["must_fix_hunks"], r["hunks_with_a_comment"], r["blocking_on_a_hunk"]), (2, 3, 2))
        self.assertEqual(r["hunks_where_context_gains_a_blocking_comment"], 1)

    def test_the_models_answers(self):
        answer = self.report["answers"]["zero-shot 7B"]
        self.assertEqual(answer["yes"]["must-fix comment on the hunk"], [2, 2])
        self.assertEqual(answer["other_hunks"], [1, 3])
        self.assertAlmostEqual(answer["must_fix_minus_other"][0], 1 - 1 / 3)
        self.assertNotIn("tabular, logistic", self.report["answers"])     # it gives a score, not a Yes or No

    def test_the_ranking(self):
        self.assertEqual(self.report["ranking"]["zero-shot 7B"]["must_fix"][0], 1.0)
        self.assertEqual(self.report["ranking"]["changed lines"]["must_fix"][0], 1.0)

    def test_the_budget(self):
        to_file = self.report["budget"]["H03_file"]
        self.assertEqual((to_file["no_context"], to_file["every_hunk"], to_file["only_the_must_fix_hunks"]), (1, 2, 2))
        self.assertEqual((to_file["every_hunk_gains"], to_file["every_hunk_loses"]), (1, 0))
        # extra tokens to the file level: 40, 4, 20, 50, 16 (130 in all); 30% is 39: the model's first hunk (40) does not fit
        self.assertEqual(to_file["selectors"]["zero-shot 7B"]["must_fix_up"], [0, 2])
        to_function = self.report["budget"]["H02_function"]      # extra 20, 2, 10, 30, 8 (70 in all); 30% is 21: hunk 1 fits
        self.assertEqual(to_function["selectors"]["zero-shot 7B"]["must_fix_up"], [1, 2])
        self.assertEqual(to_function["selectors"]["zero-shot 7B"]["covered"], 1)
        self.assertEqual(to_function["selectors"]["zero-shot 7B"]["random_at_least"], 1.0)   # every order covers the one comment
        self.assertIn("cheapest first", to_function["selectors"])

    def test_the_report_prints_and_draws(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()) as out:
            must_fix.print_report(self.report)
        self.assertIn("must-fix hunks minus the other hunks", out.getvalue())
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "figure.png"
            must_fix.figure(self.report, path)
            self.assertGreater(path.stat().st_size, 10_000)


if __name__ == "__main__":
    unittest.main()
