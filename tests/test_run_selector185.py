"""The runner of the selector notebooks on the full run (scripts/analysis/run_selector185.py):
the input files it builds, and the notebook lines it stands in for."""
import csv
import importlib.util
import json
import sys
import tempfile
import unittest
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "analysis"))

spec = importlib.util.spec_from_file_location("run_selector185", PROJECT_ROOT / "scripts" / "analysis" / "run_selector185.py")
runner = importlib.util.module_from_spec(spec)
sys.modules["run_selector185"] = runner
spec.loader.exec_module(runner)

LEVELS = runner.LEVELS


def level(tokens: int, text: str) -> dict:
    return {"input_tokens": tokens, "context_tokens": tokens - 10, "items": 1, "same_as_parent": False, "capped": False,
            "included_by_kind": {"function": 1}, "included": ["x"], "sections": ["y"], "text": text}


def write_fixture(root: Path) -> tuple[Path, Path, Path]:
    """Two PRs. o__r__1 has two reviewed hunks and one that is too long; o__s__2 has one hunk."""
    run, contexts, labels = root / "run", root / "context_levels.jsonl", root / "labels.jsonl"
    hunks = {"o__r__1": ["o__r__1__h0001", "o__r__1__h0002", "o__r__1__h0003"], "o__s__2": ["o__s__2__h0001"]}
    covered = {"o__r__1__h0001": ([], [0], [0], [0, 1]), "o__r__1__h0002": ([1], [1], [], [1]), "o__s__2__h0001": ([0], [0], [0], [0])}
    with contexts.open("w", encoding="utf-8") as handle:
        for pr, ids in hunks.items():
            entry = {"id": pr, "status": "ok", "language": "Go", "stats": {"changed_files": 1}, "graph": {"nodes": 5, "edges": 7},
                     "audit": {"big": True}, "fetch_log": ["..."],
                     "hunks": [{"id": h, "index": i, "file": "a.go", "new_start": 10 * i, "old_start": 10 * i, "old_count": 1,
                                "new_count": 2, "stats": {"added_lines": 2, "removed_lines": 1},
                                "levels": {**{lv: level(100 * (n + 1), f"{lv} text") for n, lv in enumerate(LEVELS)},
                                           "H10_all": level(900, "all text")}} for i, h in enumerate(ids, 1)]}
            handle.write(json.dumps(entry) + "\n")
    run.mkdir()
    with (run / "hunk_table.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, ["entry", "pr", "level", "input_tokens", "findings", "output_tokens", "review_status", "references_covered"])
        writer.writeheader()
        for pr, ids in hunks.items():
            for h in ids:
                for n, lv in enumerate(LEVELS):
                    ok = h in covered
                    writer.writerow({"entry": h, "pr": pr, "level": lv, "input_tokens": 100 * (n + 1), "findings": 1, "output_tokens": 5,
                                     "review_status": "ok" if ok else "too_long", "references_covered": len(covered[h][n]) if ok else 0})
                    if ok:
                        folder = run / "judgments" / h
                        folder.mkdir(parents=True, exist_ok=True)
                        (folder / f"{lv}.json").write_text(json.dumps({"covered_indices": covered[h][n], "matched_generated": len(covered[h][n])}))
    with (run / "coverage.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, ["entry", "level", "reference", "covered", "is_ai_comment", "source_model", "context", "category", "path"])
        writer.writeheader()
        for pr, ids in hunks.items():
            for index in range(2 if pr == "o__r__1" else 1):
                for n, lv in enumerate(LEVELS):
                    hit = any(index in covered[h][n] for h in ids if h in covered)
                    writer.writerow({"entry": pr, "level": lv, "reference": index, "covered": str(hit), "is_ai_comment": "True",
                                     "source_model": "", "context": "Diff Level", "category": "Code Defect", "path": "a.go"})
    labels.write_text("\n".join(json.dumps(r) for r in ({"pr": "o__r__1", "reference": 0, "tier": "blocking"},
                                                       {"pr": "o__r__1", "reference": 1, "tier": None, "error": "unreadable reply"},
                                                       {"pr": "o__s__2", "reference": 0, "tier": "nit"})), encoding="utf-8")
    return run, contexts, labels


class InputTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(dir=PROJECT_ROOT / "tests")     # the manifest stores paths under the project
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.run, self.contexts, self.labels = write_fixture(self.root)

    def test_the_three_files_and_the_manifest_are_written(self):
        out = self.root / "files"
        manifest = runner.build_inputs(self.run, self.contexts, self.labels, out)
        self.assertEqual((manifest["prs"], manifest["repositories"], manifest["hunks"], manifest["references"]), (2, 2, 3, 3))
        self.assertEqual(manifest["left_out"], ["o__r__1__h0003"])
        self.assertEqual(manifest["tiers"], {"blocking": 1, "none": 1, "nit": 1})
        # References covered per level, and hunks where a wider level covers more (h0001 only).
        self.assertEqual(manifest["expected"], {"fixed_covered": [2, 3, 2, 3], "positives": 1})
        outcomes = json.loads((out / "hunk_outcomes.json").read_text())
        self.assertEqual(outcomes["levels"], LEVELS)
        self.assertEqual([r["hunk"] for r in outcomes["rows"]], ["o__r__1__h0001", "o__r__1__h0002", "o__s__2__h0001"])
        self.assertNotIn("pr_refs", outcomes["rows"][0])
        self.assertEqual(outcomes["refs"]["o__r__1"]["0"]["tier"], "blocking")
        self.assertIsNone(outcomes["refs"]["o__r__1"]["1"]["tier"])
        self.assertEqual(manifest["sha256"]["coverage.csv"], runner.sha256(out / "coverage.csv"))

    def test_the_slim_contexts_keep_the_reviewed_hunks_and_levels_only(self):
        out = self.root / "files"
        runner.build_inputs(self.run, self.contexts, self.labels, out)
        entries = [json.loads(line) for line in (out / "context_levels_slim.jsonl").read_text().splitlines()]
        self.assertEqual([e["id"] for e in entries], ["o__r__1", "o__s__2"])
        self.assertNotIn("audit", entries[0])
        self.assertEqual([h["id"] for h in entries[0]["hunks"]], ["o__r__1__h0001", "o__r__1__h0002"])
        levels = entries[0]["hunks"][0]["levels"]
        self.assertEqual(list(levels), LEVELS)
        self.assertEqual(levels["H01_min"]["text"], "H01_min text")
        self.assertNotIn("text", levels["H03_file"])
        self.assertNotIn("included", levels["H01_min"])
        self.assertEqual(levels["H04_callees1"]["included_by_kind"], {"function": 1})

    def test_a_set_of_pull_requests_can_be_kept(self):
        out = self.root / "one"
        manifest = runner.build_inputs(self.run, self.contexts, self.labels, out, lambda entry: entry == "o__s__2")
        self.assertEqual((manifest["prs"], manifest["hunks"], manifest["references"], manifest["left_out"]), (1, 1, 1, []))
        self.assertEqual(manifest["expected"], {"fixed_covered": [1, 1, 1, 1], "positives": 0})
        with (out / "coverage.csv").open(newline="") as handle:
            self.assertEqual({row["entry"] for row in csv.DictReader(handle)}, {"o__s__2"})


class NotebookTests(unittest.TestCase):
    def test_each_replaced_line_is_in_both_notebooks_exactly_once(self):
        for name in runner.NOTEBOOKS.values():
            notebook = json.loads((PROJECT_ROOT / "notebooks" / name).read_text(encoding="utf-8"))
            used: Counter = Counter()
            for cell in notebook["cells"]:
                if cell["cell_type"] == "code":
                    runner.patched("".join(cell["source"]), used)
            self.assertEqual({old: used[old] for old, _ in runner.REPLACEMENTS}, {old: 1 for old, _ in runner.REPLACEMENTS}, name)

    def test_a_replaced_check_compares_with_the_new_sets_counts(self):
        used: Counter = Counter()
        source = runner.patched('assert LABEL != "wider" or Y.sum() == 190, Y.sum()', used)
        self.assertEqual(source, 'assert LABEL != "wider" or Y.sum() == EXPECT_POSITIVES, (Y.sum(), EXPECT_POSITIVES)')
        names = {"LABEL": "wider", "Y": [1, 1, 0], "EXPECT_POSITIVES": 2}
        exec(source.replace("Y.sum()", "sum(Y)"), names)
        with self.assertRaises(AssertionError):
            exec(source.replace("Y.sum()", "sum(Y)"), {**names, "EXPECT_POSITIVES": 3})


class LlmOptionTests(unittest.TestCase):
    def options(self, *argv):
        import argparse
        parser = argparse.ArgumentParser()
        for flag in ("--skip-zero-shot", "--skip-embeddings", "--skip-lora"):
            parser.add_argument(flag, action="store_true")
        for flag in ("--zero-shot-sizes", "--embedding-sizes"):
            parser.add_argument(flag, nargs="+")
        for flag in ("--zero-shot-batch", "--embedding-batch"):
            parser.add_argument(flag, type=int)
        return parser.parse_args(list(argv))

    def test_sizes_become_the_notebooks_model_names(self):
        self.assertEqual(runner.model_names(["1.5", "3b", "7B"]),
                         ["Qwen/Qwen2.5-Coder-1.5B-Instruct", "Qwen/Qwen2.5-Coder-3B-Instruct", "Qwen/Qwen2.5-Coder-7B-Instruct"])

    def test_the_names_are_ones_the_notebook_lists(self):
        notebook = json.loads((PROJECT_ROOT / "notebooks" / runner.NOTEBOOKS["llm"]).read_text(encoding="utf-8"))
        source = "".join("".join(cell["source"]) for cell in notebook["cells"] if cell["cell_type"] == "code")
        for name in runner.model_names(["0.5", "1.5", "3", "7", "14"]):
            self.assertIn(f'"{name}"', source)
        for setting in ("ZS_MODELS", "EMB_MODELS", "PLOT_METHODS", "ZS_BATCH", "EMB_BATCH", "RUN_ZERO_SHOT", "RUN_EMBED", "RUN_FINETUNE"):
            self.assertIn(setting, source)

    def test_no_option_changes_nothing(self):
        self.assertEqual(runner.llm_settings(self.options()), {})

    def test_sizes_and_skips_set_the_lists_the_switches_and_the_figure(self):
        settings = runner.llm_settings(self.options("--zero-shot-sizes", "1.5", "7", "--skip-embeddings", "--skip-lora",
                                                    "--zero-shot-batch", "2"))
        self.assertEqual(settings, {"RUN_EMBED": False, "RUN_FINETUNE": False,
                                    "ZS_MODELS": ["Qwen/Qwen2.5-Coder-1.5B-Instruct", "Qwen/Qwen2.5-Coder-7B-Instruct"],
                                    "PLOT_METHODS": ["tabular, logistic", "zero-shot 1.5B", "zero-shot 7B"], "ZS_BATCH": 2})


if __name__ == "__main__":
    unittest.main()
