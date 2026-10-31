"""Synthetic paired evidence with known outcomes; no dataset or weights needed."""
import csv
import hashlib
import importlib.util
import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.evaluation.analysis import analyze_predictions
from src.evaluation.reporting import report_markdown, write_report
from src.evaluation.splits import digest


def predictions():
    # a: both correct, b: improved, c: regressed, d: both wrong.
    labels = [False, True, False, True]
    conditions = {"base": [False, False, False, None],
                  "adapter": [False, True, True, False],
                  "majority": [False] * 4}
    rows = []
    for name, values in conditions.items():
        for i, (label, prediction) in enumerate(zip(labels, values)):
            rows.append({"id": "abcd"[i], "repository": "repo/one" if i < 2 else "repo/two",
                         "pr_number": i, "model": name, "label": label, "prediction": prediction,
                         "raw_output": "invalid" if prediction is None else json.dumps({"is_merged": prediction}),
                         "input_tokens": 0 if name == "majority" else 100 if name == "base" else 200,
                         "output_tokens": 0 if name == "majority" else 8,
                         "latency_seconds": 0 if name == "majority" else .1,
                         "synthetic": True})
    return rows


class AnalysisTests(unittest.TestCase):
    def analyze(self, rows=None, **kwargs):
        return analyze_predictions(predictions() if rows is None else rows, bootstrap_samples=100, **kwargs)

    def test_known_metrics_and_invalids_never_become_false(self):
        result = self.analyze()
        scores = {row["model"]: row for row in result["metrics"]}
        self.assertEqual(scores["base"]["accuracy"], .5)
        self.assertEqual(scores["base"]["macro_f1"], .4)
        self.assertEqual(scores["base"]["malformed_rate"], .25)
        self.assertEqual(scores["base"]["confusion_matrix"], [[2, 0, 0], [1, 0, 1]])
        self.assertEqual(scores["adapter"]["macro_f1"], .5)
        rows = predictions()
        for row in rows:
            if row["id"] == "d":
                row["label"] = False
        result = self.analyze(rows)
        base = next(m for m in result["metrics"] if m["model"] == "base")
        self.assertEqual(base["accuracy"], .5)  # Invalid negative-label answer is still incorrect.

    def test_paired_categories_and_difference_identity(self):
        result = self.analyze()
        pair = next(row for row in result["paired_comparisons"] if row["model"] == "adapter")
        for key in ("both_correct", "improved", "regressed", "both_wrong"):
            self.assertEqual(pair[key], 1)
        self.assertEqual(pair["accuracy_difference"], (pair["improved"] - pair["regressed"]) / 4)
        self.assertAlmostEqual(pair["macro_f1_difference"], .1)

    def test_reject_partial_duplicate_and_different_label_identity(self):
        with self.assertRaisesRegex(ValueError, "same example IDs"):
            self.analyze(predictions()[:-1])
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            self.analyze(predictions() + [predictions()[0]])
        for field, value in (("label", True), ("repository", "other"), ("pr_number", 999)):
            rows = predictions()
            rows[0][field] = value
            with self.assertRaises(ValueError):
                self.analyze(rows)
        with self.assertRaisesRegex(ValueError, "required_models"):
            self.analyze(required_models=["base", "adapter"])

    def test_reject_ambiguous_types_and_corrupt_cache_flags(self):
        for field, value in (("prediction", 0), ("label", "false"), ("input_tokens", True),
                             ("input_tokens", 1.5), ("latency_seconds", float("nan")),
                             ("latency_seconds", -1), ("correct", False), ("valid", False),
                             ("raw_output", '{"is_merged":true}')):
            rows = predictions()
            rows[0][field] = value
            with self.assertRaises(ValueError, msg=field):
                self.analyze(rows)
        with self.assertRaises(ValueError):
            self.analyze([])
        rows = predictions()
        del rows[0]["prediction"]
        with self.assertRaisesRegex(ValueError, "Missing"):
            self.analyze(rows)

    def test_protocol_checks_exact_ids_and_hash(self):
        protocol = {"ids": list("abcd"), "split": "validation", "seed": 42}
        rows = [dict(row, protocol_hash=digest(protocol)) for row in predictions()]
        result = self.analyze(rows, protocol=protocol)
        self.assertTrue(result["protocol_verified"])
        self.assertEqual(result["split"], "validation")
        with self.assertRaisesRegex(ValueError, "protocol hash"):
            self.analyze(rows, protocol={**protocol, "seed": 43})
        with self.assertRaisesRegex(ValueError, "IDs"):
            self.analyze(rows, protocol={**protocol, "ids": list("abc")})
        with self.assertRaises(ValueError):
            self.analyze(rows, protocol=[])
        rows[0].pop("protocol_hash")
        with self.assertRaisesRegex(ValueError, "protocol hashes"):
            self.analyze(rows)

    def test_bootstrap_is_paired_order_independent_and_keeps_clusters(self):
        first = self.analyze()
        self.assertEqual(first, self.analyze(list(reversed(predictions()))))
        # Force every replicate to draw repo/one twice. Its two examples must
        # remain together: base=.5, adapter=1, paired accuracy difference=.5.
        with patch("src.evaluation.analysis.random.Random.choice", return_value="repo/one"):
            bootstrap = self.analyze()["bootstrap"]
        self.assertEqual(bootstrap["model_intervals"]["base"]["accuracy"], [.5, .5])
        self.assertEqual(bootstrap["model_intervals"]["adapter"]["accuracy"], [1, 1])
        self.assertEqual(bootstrap["difference_intervals"]["adapter"]["accuracy"], [.5, .5])

    def test_single_repository_does_not_claim_an_interval(self):
        result = self.analyze([dict(row, repository="only/repo") for row in predictions()])
        self.assertEqual(result["bootstrap"]["status"], "insufficient_repositories")
        self.assertEqual(result["bootstrap"]["difference_intervals"], {})
        self.assertIn("Fewer than two repositories", report_markdown(result))

    def test_incomplete_costs_are_not_averaged_and_majority_is_not_a_frontier(self):
        rows = predictions()
        del rows[0]["input_tokens"]
        result = self.analyze(rows)
        base = next(row for row in result["metrics"] if row["model"] == "base")
        self.assertIsNone(base["mean_input_tokens"])
        self.assertEqual(base["recorded_input_tokens"], 3)
        self.assertEqual(result["cost_omissions"], ["base"])
        self.assertNotIn("majority", [point["model"] for point in result["cost_points"]])
        result = self.analyze(cost_field="extra_context_tokens")
        self.assertEqual(result["cost_points"], [])
        self.assertEqual(result["cost_omissions"], ["adapter", "base"])

    def test_empirical_frontier_uses_quality_and_cost(self):
        points = {point["model"]: point for point in self.analyze()["cost_points"]}
        self.assertTrue(points["base"]["on_frontier"])
        self.assertFalse(points["adapter"]["on_frontier"])

    def test_report_outputs_checksums_and_preserves_old_report(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            write_report(self.analyze(), output, provenance={"synthetic": True})
            self.assertIn("SYNTHETIC DEMO", (output / "report.md").read_text())
            self.assertIn("unverified", (output / "report.md").read_text())
            manifest = json.loads((output / "analysis_manifest.json").read_text())
            for name, checksum in manifest["artifacts"].items():
                self.assertEqual(hashlib.sha256((output / name).read_bytes()).hexdigest(), checksum)
            with (output / "metrics.csv").open() as stream:
                self.assertEqual(len(list(csv.DictReader(stream))), 3)
            with self.assertRaises(FileExistsError):
                write_report(self.analyze(), output, provenance={})

    def test_cli_reads_notebook_schema_without_model_dependencies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "predictions.jsonl"
            protocol = {"ids": list("abcd"), "split": "validation"}
            rows = [dict(row, protocol_hash=digest(protocol)) for row in predictions()]
            source.write_text("".join(json.dumps(row) + "\n" for row in rows))
            (root / "protocol.json").write_text(json.dumps(protocol))
            command = [sys.executable, "scripts/analyze_predictions.py", "--predictions", str(source),
                       "--output", str(root / "report"), "--bootstrap-samples", "20",
                       "--models", "majority", "base", "adapter"]
            run = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("SYNTHETIC DEMO", (root / "report/report.md").read_text())
            result = json.loads((root / "report/summary.json").read_text())
            self.assertEqual(result["split"], "validation")
            rerun = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(rerun.returncode, 2)
            self.assertIn("new or empty", rerun.stderr)

    @unittest.skipUnless(importlib.util.find_spec("sklearn"), "Optional scikit-learn parity check")
    def test_metrics_match_existing_sklearn_comparison(self):
        from src.evaluation.comparison import summarize
        rng = random.Random(42)
        for _ in range(20):
            rows = predictions()
            for row in rows:
                row["prediction"] = rng.choice([None, False, True])
                row.pop("raw_output")
            expected = {row["model"]: row for row in summarize(rows)}
            for actual in self.analyze(rows)["metrics"]:
                for field, value in expected[actual["model"]].items():
                    if field != "model":
                        self.assertAlmostEqual(actual[field], value)

    @unittest.skipUnless(importlib.util.find_spec("matplotlib"), "Optional Matplotlib export check")
    def test_all_plot_exports_from_synthetic_predictions(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report"
            write_report(self.analyze(), output, provenance={"synthetic": True}, plots=True)
            for name in ("scores", "confusion_matrices", "paired_outcomes", "paired_intervals", "accuracy_cost"):
                self.assertGreater((output / f"{name}.png").stat().st_size, 1000)
                self.assertTrue((output / f"{name}.pdf").read_bytes().startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
