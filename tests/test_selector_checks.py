"""The selector checks on a finished per-hunk run (scripts/analysis/selector_checks.py) and the
run's report (scripts/analysis/level_run_report.py): rows, labels, gains and losses, totals."""
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

LEVELS = ["H01_min", "H02_function", "H03_file", "H04_callees1"]


def load_script(name: str):
    spec = importlib.util.spec_from_file_location(name, PROJECT_ROOT / "scripts" / "analysis" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


checks = load_script("selector_checks")
report = load_script("level_run_report")


def row(hunk: str, covered: list[list[int]], tokens=(100, 200, 400, 800), **facts) -> dict:
    base = {"hunk": hunk, "pr": hunk.split("__h")[0], "language": "Go", "file": "pkg/a.go", "added": 3, "removed": 1,
            "diff_chars": 120, "pr_hunks": 4, "file_hunks": 2, "changed_symbols": 1, "callees1": 2, "callers1": 0, "tests1": 0,
            "search_hits": 5, "new_start": 10, "new_file": False}
    base.update(facts)
    base["levels"] = {lv: {"tokens": t, "findings": 1, "covered": c, "matched": len(c), "out": 50}
                      for lv, t, c in zip(LEVELS, tokens, covered)}
    return base


ROWS = [
    row("o__r__1__h0001", [[], [0], [0], [0, 1]]),    # every wider level covers more
    row("o__r__1__h0002", [[2], [], [2], [2]]),       # the function level covers fewer
    row("o__r__1__h0003", [[], [], [], []]),          # nothing at any level
    row("o__r__2__h0001", [[0], [0], [0, 1], [0]]),   # only the file covers more
]


class LabelTests(unittest.TestCase):
    def test_a_hunk_is_positive_when_some_wider_level_covers_more(self):
        self.assertEqual(checks.labels(ROWS, LEVELS), {"wider": [1, 0, 0, 1], "file": [1, 0, 0, 1]})

    def test_the_file_label_needs_the_file_level(self):
        self.assertEqual(set(checks.labels(ROWS, LEVELS[:2])), {"wider"})

    def test_gains_and_losses_are_counted_per_level_against_the_first(self):
        self.assertEqual(checks.gains_losses(ROWS, LEVELS), {"H02_function": (1, 1), "H03_file": (2, 0), "H04_callees1": (1, 0)})

    def test_a_prs_references_are_the_union_over_its_hunks(self):
        self.assertEqual(checks.union_covered(ROWS, lambda r: "H04_callees1"), (4, 3200))  # PR 1: {0, 1, 2}; PR 2: {0}
        self.assertEqual(checks.union_covered(ROWS, lambda r: "H01_min"), (2, 400))

    def test_hindsight_takes_the_cheapest_level_that_covers_the_most(self):
        self.assertEqual([checks.hindsight_level(r, LEVELS) for r in ROWS], ["H04_callees1", "H01_min", "H01_min", "H03_file"])

    def test_features_are_the_same_length_for_every_hunk(self):
        languages = ["Go", "Python"]
        first, second = checks.features(ROWS[0], LEVELS, languages), checks.features(row("o__r__3__h0001", [[]] * 4, language="Python",
                                                                                         file="tests/test_a.py"), LEVELS, languages)
        self.assertEqual(len(first), len(second))
        self.assertEqual(first[-2:], [1.0, 0.0])
        self.assertEqual(second[-3:], [1.0, 0.0, 1.0])  # a test path, then the language

    def test_the_repository_is_the_pr_id_without_its_number(self):
        self.assertEqual(checks.repository("vllm-project__vllm__19756"), "vllm-project__vllm")


class RowTests(unittest.TestCase):
    def test_rows_are_built_for_the_reviewed_hunks_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            run = Path(tmp) / "run"
            hunks = [{"id": f"o__r__1__h000{i}", "file": "a.go", "new_start": 5 * i, "old_start": 5 * i,
                      "stats": {"added_lines": i, "removed_lines": 0, "diff_chars": 40, "changed_symbols": 1,
                                "callees_by_hop": {"1": 2}, "callers_by_hop": {}, "search_hits": 3}} for i in (1, 2, 3)]
            contexts = Path(tmp) / "context_levels.jsonl"
            contexts.write_text(json.dumps({"id": "o__r__1", "language": "Go", "hunks": hunks}) + "\n", encoding="utf-8")
            reviewed = [h["id"] for h in hunks[:2]]  # the third hunk is in a file without a reference
            run.mkdir()
            with (run / "hunk_table.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, ["entry", "level", "input_tokens", "findings", "output_tokens"])
                writer.writeheader()
                for hunk in reviewed:
                    for n, level in enumerate(LEVELS):
                        writer.writerow({"entry": hunk, "level": level, "input_tokens": 100 * (n + 1), "findings": 1, "output_tokens": 9})
                        folder = run / "judgments" / hunk
                        folder.mkdir(parents=True, exist_ok=True)
                        (folder / f"{level}.json").write_text(json.dumps({"covered_indices": [0] if n else [], "matched_generated": n}))
            with (run / "coverage.csv").open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, ["entry", "level", "reference", "covered", "is_ai_comment", "context", "category", "path"])
                writer.writeheader()
                for level in LEVELS:
                    writer.writerow({"entry": "o__r__1", "level": level, "reference": 0, "covered": level != "H01_min",
                                     "is_ai_comment": "False", "context": "Diff Level", "category": "Code Defect", "path": "a.go"})
            table = checks.build_rows(contexts, run, LEVELS)
        self.assertEqual([r["hunk"] for r in table["rows"]], reviewed)
        first = table["rows"][0]
        self.assertEqual((first["pr_hunks"], first["file_hunks"], first["callees1"], first["callers1"], first["pr_refs"]), (3, 3, 2, 0, 1))
        self.assertEqual(first["levels"]["H03_file"], {"tokens": 300, "findings": 1, "covered": [0], "matched": 2, "out": 9})
        self.assertTrue(table["refs"]["o__r__1"]["0"]["human"])
        self.assertEqual(checks.labels(table["rows"], LEVELS)["wider"], [1, 1])


class ReportTests(unittest.TestCase):
    TABLE = [
        {"entry": "a__b__1", "level": "H01_min", "expected": "4", "covered": "1", "generated": "5", "matched_generated": "1",
         "input_tokens": "100", "output_tokens": "10", "human_expected": "1", "human_covered": "0", "ai_expected": "3", "ai_covered": "1"},
        {"entry": "a__b__2", "level": "H01_min", "expected": "6", "covered": "4", "generated": "5", "matched_generated": "4",
         "input_tokens": "300", "output_tokens": "30", "human_expected": "2", "human_covered": "1", "ai_expected": "4", "ai_covered": "3"},
        {"entry": "a__b__1", "level": "H02_function", "expected": "4", "covered": "3", "generated": "4", "matched_generated": "3",
         "input_tokens": "200", "output_tokens": "10", "human_expected": "1", "human_covered": "1", "ai_expected": "3", "ai_covered": "2"},
        {"entry": "a__b__2", "level": "H02_function", "expected": "6", "covered": "4", "generated": "6", "matched_generated": "3",
         "input_tokens": "600", "output_tokens": "30", "human_expected": "2", "human_covered": "1", "ai_expected": "4", "ai_covered": "3"},
    ]
    SCORES = [
        {"entry": "a__b__1", "level": "H01_min", "unlabelled": "0", "blocking_references": "1", "should_fix_references": "1",
         "nit_references": "2", "blocking_covered": "0", "should_fix_covered": "1", "nit_covered": "0", "reference_weight": "8.0",
         "covered_weight": "2.0"},
        {"entry": "a__b__2", "level": "H01_min", "unlabelled": "1", "blocking_references": "0", "should_fix_references": "2",
         "nit_references": "3", "blocking_covered": "0", "should_fix_covered": "2", "nit_covered": "2", "reference_weight": "5.5",
         "covered_weight": "5.0"},
    ]

    def test_recall_and_precision_are_pooled_over_the_prs(self):
        totals = report.level_totals(self.TABLE, ["H01_min", "H02_function"])
        self.assertEqual((totals["H01_min"]["covered"], totals["H01_min"]["expected"], totals["H01_min"]["prs"]), (5, 10, 2))
        self.assertAlmostEqual(totals["H01_min"]["recall"], 0.5)
        self.assertAlmostEqual(totals["H02_function"]["precision"], 0.6)
        only_first = report.level_totals(self.TABLE, ["H01_min"], lambda entry: entry.endswith("__1"))
        self.assertEqual((only_first["H01_min"]["covered"], only_first["H01_min"]["prs"]), (1, 1))

    def test_the_score_is_the_covered_weight_over_the_reference_weight(self):
        scores = report.level_scores(self.SCORES, ["H01_min"])["H01_min"]
        self.assertAlmostEqual(scores["score"], 7.0 / 13.5)
        self.assertEqual((scores["should_fix"], scores["nit"], scores["unlabelled"]), ((3, 3), (2, 5), 1))

    def test_the_bootstrap_compares_each_level_with_the_first_on_the_same_prs(self):
        scores = self.SCORES + [dict(r, level="H02_function", covered_weight=str(float(r["covered_weight"]) + 1)) for r in self.SCORES]
        result = report.paired_bootstrap(self.TABLE, scores, ["H01_min", "H02_function"], draws=200)["H02_function"]
        mean, low, high, at_or_below_zero = result["recall"]
        self.assertTrue(low <= mean <= high)
        self.assertGreater(mean, 0)            # the function level covers 7 of 10 against 5 of 10
        self.assertGreater(result["score"][1], 0)  # every PR gains weight, so every draw is above zero
        self.assertEqual(result["score"][3], 0.0)

    def test_the_yield_counts_positive_hunks_per_pr_and_per_hunk(self):
        result = report.yield_of(ROWS, LEVELS)
        self.assertEqual((result["positives"], result["prs"], result["hunks"]), (2, 2, 4))
        self.assertAlmostEqual(result["per_pr"], 1.0)
        self.assertAlmostEqual(result["per_hunk"], 0.5)


if __name__ == "__main__":
    unittest.main()
