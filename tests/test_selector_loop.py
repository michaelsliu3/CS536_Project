"""Tests for scripts/analysis/selector_loop.py: the loop's rule, what its reviews cover together,
what they cost, and the baselines at equal cost, on a small hand-made set."""
import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "analysis"))
spec = importlib.util.spec_from_file_location("selector_loop", PROJECT_ROOT / "scripts" / "analysis" / "selector_loop.py")
loop = importlib.util.module_from_spec(spec)
spec.loader.exec_module(loop)

LEVELS = loop.LEVELS


def row(hunk: str, pr: str, lines: int, covered: list[list[int]]) -> dict:
    return {"hunk": hunk, "pr": pr, "language": "Go", "file": "a/b.go", "added": lines, "removed": 0, "diff_chars": 40 * lines,
            "pr_hunks": 3, "file_hunks": 1, "changed_symbols": 1, "callees1": 1, "callers1": 0, "tests1": 0, "search_hits": 2,
            "new_start": 10, "new_file": False,
            "levels": {level: {"tokens": 100 * (l + 1), "covered": covered[l]} for l, level in enumerate(LEVELS)}}


def write_fixture(root: Path) -> tuple[Path, Path, Path]:
    """Two repositories. o__a__1 has references 0 (blocking), 1 (nit) and 2 (should fix); o__b__2 has reference 0 (nit).
    Hunk a1 gains reference 1 at the function level and reference 2 at the callees level."""
    rows = [row("o__a__1__h0001", "o__a__1", 50, [[0], [0, 1], [0], [2]]),
            row("o__a__1__h0002", "o__a__1", 2, [[], [], [], []]),
            row("o__b__2__h0001", "o__b__2", 5, [[0], [0], [0], [0]]),
            row("o__b__2__h0002", "o__b__2", 1, [[], [0], [], []])]
    refs = {"o__a__1": {"0": {"tier": "blocking"}, "1": {"tier": "nit"}, "2": {"tier": "should_fix"}}, "o__b__2": {"0": {"tier": "nit"}}}
    outcomes = root / "hunk_outcomes.json"
    outcomes.write_text(json.dumps({"rows": rows, "refs": refs, "levels": LEVELS}), encoding="utf-8")
    run = root / "run"
    for r in rows:
        for l, level in enumerate(LEVELS):
            path = run / "reviews" / r["hunk"] / f"{level}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            notes = ["Please verify that the caller handles nil.", "This is not shown in the diff."][:1 + (l == 0)]
            path.write_text(json.dumps({"reply": {"prompt_tokens": 1000 * (l + 1), "completion_tokens": 100},
                                        "parsed": {"findings": [{"note": note} for note in notes], "off_hunk_findings": []}}), encoding="utf-8")
    folds = root / "folds.csv"
    with folds.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["hunk", "fold"])
        writer.writerows([[r["hunk"], int(r["pr"] == "o__b__2")] for r in rows])
    return outcomes, run, folds


class Fixture(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.data = loop.load(*write_fixture(Path(self.folder.name)))


class Reading(Fixture):
    def test_the_review_facts_count_what_a_finding_says(self):
        first = self.data["reviews"][0][0]
        self.assertEqual((first["in"], first["out"], first["findings"]), (1000, 100, 2))
        self.assertEqual((first["asks_to_check"], first["cannot_see"]), (1, 1))
        self.assertEqual(self.data["reviews"][0][1]["cannot_see"], 0)

    def test_the_references_get_their_severity_weight(self):
        self.assertEqual(self.data["weight"].tolist(), [5.0, 0.5, 2.0, 0.5])      # o__a__1 0, 1, 2 and o__b__2 0
        self.assertEqual((self.data["prs"], self.data["references"]), (2, 4))
        self.assertEqual(self.data["fold"].tolist(), [0, 0, 1, 1])
        self.assertEqual(self.data["lines"].tolist(), [50, 2, 5, 1])

    def test_a_request_costs_its_input_and_output_at_the_price_list(self):
        self.assertAlmostEqual(self.data["price"][0, 0], (1000 * 2 + 100 * 10) / 1e6)
        self.assertAlmostEqual(self.data["price"][0].sum(), (10000 * 2 + 400 * 10) / 1e6)   # all four requests of the hunk
        self.assertEqual(self.data["tokens"][0].tolist(), [100, 200, 300, 400])


class Gate(Fixture):
    def test_the_next_level_adds_a_catch_only_for_a_reference_no_review_so_far_covers(self):
        self.assertEqual(loop.adds_catch(self.data, 0).tolist(), [True, False, False, True])
        self.assertEqual(loop.adds_catch(self.data, 1).tolist(), [False, False, False, False])   # the file level repeats reference 0
        self.assertEqual(loop.adds_catch(self.data, 2).tolist(), [True, False, False, False])

    def test_the_gates_inputs_can_be_the_facts_the_review_or_both(self):
        facts, review, both = (loop.gate_inputs(self.data, 0, name) for name in ("facts", "review", "both"))
        self.assertEqual(both.shape[1], facts.shape[1] + review.shape[1])
        self.assertEqual(review[0].tolist(), [2, 0, 100, len("Please verify that the caller handles nil.") + len("This is not shown in the diff."), 0, 1, 1])

    def test_auc(self):
        self.assertEqual(loop.auc([1, 0, 0], [3, 2, 1]), 1.0)
        self.assertEqual(loop.auc([1, 0], [5, 5]), 0.5)


class Loop(Fixture):
    def test_a_hunk_moves_up_while_its_score_is_at_least_the_price(self):
        scores = [np.array([0.9, 0.1, 0.5, 0.5]), np.array([0.6, 0.9, 0.2, 0.7]), np.array([0.1, 0.9, 0.9, 0.5])]
        self.assertEqual(loop.run_loop(scores, 0.5).tolist(), [2, 0, 1, 3])     # hunk 2 stops at the function level, hunk 1 never starts
        self.assertEqual(loop.run_loop(scores, 0.95).tolist(), [0, 0, 0, 0])

    def test_the_loop_can_begin_above_the_lowest_level(self):
        scores = [np.array([0.9, 0.1, 0.5, 0.5]), np.array([0.6, 0.9, 0.2, 0.7]), np.array([0.1, 0.9, 0.9, 0.5])]
        start = np.array([0, 1, 0, 0])
        self.assertEqual(loop.run_loop(scores, 0.5, start).tolist(), [2, 3, 1, 3])   # hunk 1 starts at function and goes on from there
        self.assertEqual(start.tolist(), [0, 1, 0, 0])                               # the start is not changed

    def test_the_selector_starts_its_top_hunks_higher_until_its_budget_is_spent(self):
        tokens = self.data["tokens"]                                                # the function level adds 100 tokens to every hunk
        score = np.array([0.2, 0.9, 0.5, 0.1])
        self.assertEqual(loop.selector_start(tokens, score, 0.3).tolist(), [0, 1, 0, 0])      # 30% of 400 buys one hunk
        self.assertEqual(loop.selector_start(tokens, score, 0.5).tolist(), [0, 1, 1, 0])
        self.assertEqual(loop.selector_start(tokens, score, 0.0).tolist(), [0, 0, 0, 0])

    def test_a_hunk_that_starts_higher_is_not_reviewed_below(self):
        start = np.array([1, 0, 0, 0])
        only = loop.evaluate(self.data, start, start)                               # hunk 0 gets its function review alone
        self.assertEqual((only["covered"], only["requests"], only["extra_requests"], only["hunks_with_more"]), (3, 4, 0, 0))
        self.assertEqual((only["starts"], only["depths"]), ([3, 1, 0, 0], [3, 1, 0, 0]))
        self.assertAlmostEqual(only["cost_per_pr"], (0.005 + 3 * 0.003) / 2)
        both = loop.evaluate(self.data, np.array([1, 0, 0, 0]))                     # the same hunk from no context: two requests
        self.assertEqual((both["requests"], both["extra_requests"]), (5, 1))
        self.assertAlmostEqual(both["cost_per_pr"], (0.003 + 0.005 + 3 * 0.003) / 2)

    def test_second_reviews_go_one_level_above_each_hunks_start(self):
        start = np.array([1, 0, 0, 3])
        order = np.array([3, 0, 1, 2])                                              # hunk 3 is at the top and is skipped
        self.assertEqual(loop.second_reviews(self.data, order, 0.008, start).tolist(), [2, 0, 0, 3])   # the file review of hunk 0 costs 0.007
        self.assertEqual(loop.second_reviews(self.data, order, 0.012, start).tolist(), [2, 1, 0, 3])

    def test_the_price_for_a_share_of_hunks(self):
        scores = np.array([0.1, 0.2, 0.3, 0.4])
        self.assertEqual((scores >= loop.price_for(scores, 0.25)).sum(), 1)
        self.assertEqual(loop.price_for(scores, 0), float("inf"))

    def test_the_reviews_of_a_hunk_are_kept_together_and_all_paid(self):
        none = loop.evaluate(self.data, np.zeros(4, dtype=int))
        self.assertEqual((none["covered"], none["requests"], none["extra_requests"]), (2, 4, 0))
        self.assertAlmostEqual(none["score"], 5.5 / 8.0)
        self.assertAlmostEqual(none["cost_per_pr"], 4 * 0.003 / 2)
        more = loop.evaluate(self.data, np.array([3, 0, 0, 1]))
        self.assertEqual((more["covered"], more["extra_requests"], more["hunks_with_more"]), (4, 4, 2))
        self.assertEqual(more["depths"], [2, 1, 0, 1])
        self.assertAlmostEqual(more["cost_per_pr"], (0.003 * 2 + 0.024 + 0.003 + 0.005) / 2)

    def test_the_gate_that_knows_the_answer_stops_where_the_next_level_adds_nothing(self):
        self.assertEqual(loop.hindsight_loop(self.data).tolist(), [1, 0, 0, 1])   # hunk 0 stops at function: the file level adds nothing

    def test_second_reviews_are_bought_in_order_until_the_dollars_run_out(self):
        order = np.array([1, 0, 2, 3])
        self.assertEqual(loop.second_reviews(self.data, order, 0.011).tolist(), [1, 1, 0, 0])   # a second review costs 0.005
        self.assertEqual(loop.second_reviews(self.data, order, 0.004).tolist(), [0, 0, 0, 0])

    def test_the_same_dollars_on_the_largest_hunks(self):
        entry = loop.evaluate(self.data, np.array([0, 0, 0, 1]))                  # the loop gave one second review, worth 0.005
        cost = loop.at_equal_cost(self.data, entry, draws=40)
        self.assertEqual((cost["largest_first_covered"], cost["largest_first_hunks_with_more"]), (3, 1))   # hunk 0 gains reference 1
        self.assertEqual(cost["random_hunks_with_more"], 1.0)
        self.assertLessEqual(cost["random_covered_mean"], 3.0)


class GivenGate(Fixture):
    def test_a_file_of_scores_stands_in_for_the_trained_gate(self):
        path = Path(self.folder.name) / "gate.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["hunk", "gate|H01_min", "gate|H02_function", "gate|H03_file"])
            writer.writerows([[hunk, 2.0 - i, -1.0, 0.5 * i] for i, hunk in enumerate(self.data["hunks"])])
        gate = loop.read_gate(path, self.data)
        self.assertEqual([g.tolist() for g in gate], [[2.0, 1.0, 0.0, -1.0], [-1.0] * 4, [0.0, 0.5, 1.0, 1.5]])
        report = loop.build_report(self.data, [0.25], draws=20, gate=gate, gate_name="a model's answer")
        self.assertEqual(report["gate"], "a model's answer")
        self.assertEqual(report["gates"][0]["auc"][0], 0.5)                  # hunks 0 and 3 gain; the scores rank them first and last
        self.assertEqual(report["gates"][0]["share_above_zero"], 0.5)
        self.assertNotIn("auc_facts_only", report["gates"][0])
        looped = next(row for row in report["rows"] if row["price"] is not None)
        self.assertEqual((looped["hunks_with_more"], looped["depths"], looped["covered"]), (1, [3, 1, 0, 0], 3))   # only the top hunk goes up

    def test_a_given_gate_is_set_beside_the_trained_one(self):
        gate = [np.array([2.0, 1.0, 0.0, -1.0]), np.full(4, -1.0), np.array([0.0, 0.5, 1.0, 1.5])]
        first = loop.build_report(self.data, [0.25], draws=20, gate=gate)["gates"][0]
        trained = loop.auc(loop.adds_catch(self.data, 0), loop.gate_scores(self.data, 0))
        self.assertEqual(first["auc_trained_gate"], trained)
        self.assertEqual(len(first["auc_minus_trained_gate"]), 3)
        self.assertAlmostEqual(first["auc_minus_trained_gate"][0], 0.5 - trained)
        self.assertEqual((first["above_zero_where_it_adds"], first["above_zero_elsewhere"]), (0.5, 0.5))   # hunks 0 and 3 gain


class Report(Fixture):
    def test_the_function_level_for_every_hunk_is_a_reference_row(self):
        report = loop.build_report(self.data, [0.25], draws=20)
        row = next(r for r in report["rows"] if r["start"] == "function")
        ones = np.ones(4, dtype=int)
        self.assertEqual((row["starts"], row["extra_requests"], row["hunks_with_more"]), ([0, 4, 0, 0], 0, 0))
        self.assertEqual(row["covered"], loop.evaluate(self.data, ones, ones)["covered"])

    def test_the_start_selectors_own_score_is_ranked_too(self):
        selector = np.array([3.0, 0.0, 1.0, 2.0])                            # ranks the two hunks that gain, 0 and 3, on top
        report = loop.build_report(self.data, [0.25], selector, "a selector", draws=20)
        self.assertEqual(report["gates"][0]["auc_start_selector"], 1.0)
        self.assertNotIn("auc_start_selector", loop.build_report(self.data, [0.25], draws=20)["gates"][0])

    def test_the_figure_is_drawn_with_and_without_a_second_gate(self):
        report = loop.build_report(self.data, [0.25, 0.5], np.array([3.0, 0.0, 1.0, 2.0]), "a selector", draws=20)
        gate = [np.array([2.0, 1.0, 0.0, -1.0]), np.full(4, -1.0), np.array([0.0, 0.5, 1.0, 1.5])]
        other = loop.build_report(self.data, [0.25, 0.5], draws=20, gate=gate, gate_name="a model")
        for name, second in (("one.png", None), ("two.png", other)):
            path = Path(self.folder.name) / "figures" / name
            loop.figure(report, path, second)
            self.assertGreater(path.stat().st_size, 10_000)

    def test_a_gate_file_must_cover_every_hunk_and_level(self):
        path = Path(self.folder.name) / "gate.csv"
        path.write_text("hunk,gate|H01_min\n" + "\n".join(f"{hunk},1" for hunk in self.data["hunks"]) + "\n", encoding="utf-8")
        with self.assertRaises(SystemExit):
            loop.read_gate(path, self.data)


if __name__ == "__main__":
    unittest.main()
