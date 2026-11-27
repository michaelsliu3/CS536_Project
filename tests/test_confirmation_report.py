"""The confirmation run's report (scripts/analysis/confirmation_run_report.py): reading the
coverage and labels, the two fixed comparisons, the exact test, the bootstrap and the reading rule."""
import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "analysis"))

spec = importlib.util.spec_from_file_location("confirmation_run_report", PROJECT_ROOT / "scripts" / "analysis" / "confirmation_run_report.py")
report = importlib.util.module_from_spec(spec)
sys.modules["confirmation_run_report"] = report
spec.loader.exec_module(report)

LEVELS = report.LEVELS


def reference(pr: str, index: int, covered: str, tier: str | None = "nit", context: str = "Diff Level") -> dict:
    """`covered` is four characters, one per level: 1 covered, 0 not."""
    return {"pr": pr, "reference": index, "repository": report.repository(pr), "context": context, "category": "Code Defect",
            "human": False, "tier": tier, "covered": {level: c == "1" for level, c in zip(LEVELS, covered)}}


class ReadingTests(unittest.TestCase):
    def test_references_are_read_with_their_tier_and_each_levels_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            run, labels = Path(folder) / "run", Path(folder) / "labels.jsonl"
            run.mkdir()
            with (run / "coverage.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.writer(handle)
                writer.writerow(["entry", "level", "reference", "covered", "is_ai_comment", "source_model", "context", "category", "path"])
                for entry, index, covered, ai in (("o__r__1", 0, "0110", "True"), ("o__r__1", 1, "1111", "False"), ("o__s__2", 0, "0000", "True")):
                    for level, c in zip(LEVELS, covered):
                        writer.writerow([entry, level, index, str(c == "1"), ai, "", "Repo Level", "Code Defect", "a.go"])
            labels.write_text("\n".join(json.dumps(r) for r in (
                {"pr": "o__r__1", "reference": 0, "tier": "blocking"}, {"pr": "o__r__1", "reference": 1, "tier": None, "error": "unreadable reply"})),
                encoding="utf-8")
            every = report.load_references(run, labels)
            self.assertEqual([(m["pr"], m["reference"], m["tier"], m["human"]) for m in every],
                             [("o__r__1", 0, "blocking", False), ("o__r__1", 1, None, True), ("o__s__2", 0, None, False)])
            self.assertEqual(every[0]["covered"], dict(zip(LEVELS, (False, True, True, False))))
            self.assertEqual(every[0]["repository"], "o__r")
            self.assertEqual(len(report.load_references(run, labels, lambda entry: entry != "o__r__1")), 1)


class StatisticsTests(unittest.TestCase):
    def test_the_exact_test_matches_known_values(self):
        self.assertAlmostEqual(report.exact_p(13, 4), 0.0490, places=4)
        self.assertAlmostEqual(report.exact_p(15, 11), 0.5572, places=4)
        self.assertEqual(report.exact_p(0, 0), 1.0)
        self.assertEqual(report.exact_p(3, 3), 1.0)

    def test_gains_and_losses_are_counted_between_two_levels(self):
        members = [reference("o__r__1", 0, "0110"), reference("o__r__1", 1, "1001"), reference("o__r__2", 0, "0010")]
        self.assertEqual({k: v for k, v in report.gained_lost(members, LEVELS[0], LEVELS[2]).items() if k != "p"}, {"gained": 2, "lost": 1})
        self.assertEqual(report.covered_counts(members), [1, 1, 2, 1])

    def test_wider_minus_none_is_the_mean_of_the_three_wider_levels(self):
        self.assertAlmostEqual(report.wider_minus_none(reference("o__r__1", 0, "0110")), 2 / 3)
        self.assertAlmostEqual(report.wider_minus_none(reference("o__r__1", 0, "1000")), -1.0)
        self.assertEqual(report.wider_minus_none(reference("o__r__1", 0, "1111")), 0.0)

    def test_the_bootstrap_draws_repositories_and_is_repeatable(self):
        members = ([reference(f"o__a__{i}", 0, "0100") for i in range(6)] + [reference(f"o__b__{i}", 0, "1000") for i in range(2)]
                   + [reference(f"o__c__{i}", 0, "0000") for i in range(4)])
        change = report.between(LEVELS[0], LEVELS[1])
        point, low, high = report.bootstrap(members, change, draws=2000)
        self.assertAlmostEqual(point, (6 - 2) / 12)
        self.assertLess(low, point)
        self.assertGreater(high, point)
        self.assertGreaterEqual(low, -1.0)      # a draw of repository b alone
        self.assertEqual(report.bootstrap(members, change, draws=2000), (point, low, high))
        self.assertIsNone(report.bootstrap([], change))

    def test_one_repository_gives_an_interval_of_no_width(self):
        members = [reference("o__a__1", 0, "0100"), reference("o__a__2", 0, "0000")]
        self.assertEqual(report.bootstrap(members, report.between(LEVELS[0], LEVELS[1]), draws=200), (0.5, 0.5, 0.5))

    def test_the_severity_score_weights_each_tier(self):
        members = [reference("o__a__1", 0, "0111", "blocking"), reference("o__a__1", 1, "1000", "nit"), reference("o__b__1", 0, "1111", None)]
        scores = report.severity_scores(members, (5.0, 2.0, 0.5), draws=200)
        self.assertAlmostEqual(scores["score"][0], 0.5 / 5.5)
        self.assertAlmostEqual(scores["score"][1], 5.0 / 5.5)
        self.assertAlmostEqual(scores["gain"][LEVELS[1]][0], 4.5 / 5.5)

    def test_the_reading_follows_the_rule_written_before_the_run(self):
        self.assertEqual(report.reading((0.05, 0.01, 0.09)), "confirmed")
        self.assertEqual(report.reading((0.03, -0.05, 0.12)), "same direction, not confirmed on its own")
        self.assertEqual(report.reading((0.0, -0.05, 0.05)), "not confirmed")
        self.assertEqual(report.reading((-0.04, -0.12, 0.05)), "not confirmed")


class RerunTests(unittest.TestCase):
    def test_disagreement_is_counted_between_runs_and_between_levels(self):
        with tempfile.TemporaryDirectory() as folder:
            runs = []
            for name, rows in (("a", {("o__r__1", 0): "0101", ("o__r__1", 1): "1111", ("o__r__2", 0): "0000"}),
                               ("b", {("o__r__1", 0): "0111", ("o__r__1", 1): "1111"})):
                run = Path(folder) / name
                run.mkdir()
                with (run / "coverage.csv").open("w", newline="", encoding="utf-8") as handle:
                    writer = csv.writer(handle)
                    writer.writerow(["entry", "level", "reference", "covered"])
                    for (entry, index), covered in rows.items():
                        for level, c in zip(LEVELS, covered):
                            writer.writerow([entry, level, index, str(c == "1")])
                        writer.writerow([entry, "H10_all", index, "True"])   # a level that is not kept is ignored
                runs.append(run)
            result = report.rerun_disagreement(runs + [Path(folder) / "missing"])
            self.assertEqual((result["prs"], result["references"]), (1, 2))      # only what both runs hold
            self.assertEqual(result["same_level_two_runs"], (1, 8))             # the file level of reference 0
            self.assertEqual(result["two_levels_one_run"], (4 + 3, 24))
            self.assertIsNone(report.rerun_disagreement(runs[:1]))


class WordingTests(unittest.TestCase):
    def test_shares_count_findings_with_each_kind_of_wording(self):
        notes = {"o__a__1__h0001": dict(zip(LEVELS, (["Please verify that x is set.", "Rename y."], ["Rename y."], ["Rename y."], ["Rename y."]))),
                 "o__b__1__h0001": dict(zip(LEVELS, (["Cannot see the caller."], ["Ensure that z holds."], [], ["Fine."])))}
        shares = report.wording_shares(notes, draws=100)
        self.assertEqual(shares["findings"], dict(zip(LEVELS, (3, 2, 1, 2))))
        self.assertEqual(shares["asks the author to check"]["share"], [1 / 3, 1 / 2, 0.0, 0.0])
        self.assertEqual(shares["says code cannot be seen"]["share"], [1 / 3, 0.0, 0.0, 0.0])


if __name__ == "__main__":
    unittest.main()
