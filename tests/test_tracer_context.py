import copy
import tempfile
import unittest
from pathlib import Path

from src.tracer_context.build import (
    Item, annotation_start, apply_limits, build_context, choose_segments, collect_items,
    is_test_path, markdown_section_end, parse_hunks, python_decorator_starts, rank_key,
    validate_config,
)

CONFIG = {
    "version": 1,
    "tracer": {"binary": "bin/tool", "workers": 4, "index_mode": "full",
               "callers": {"depth": 2, "edge_types": ["CALLS", "TESTS"]}, "callees": {"depth": 2}},
    "selection": {
        "seed_scope": "innermost", "drop_labels": ["Module", "File"], "drop_builtins": True,
        "test_globs": ["tests/**", "**/test_*.py"],
        "limits": {"changed": {"max_hop": 0, "max_items": None},
                   "callee": {"max_hop": 2, "max_items": 10}, "caller": {"max_hop": 2, "max_items": 10},
                   "test": {"max_hop": 1, "max_items": 2}, "doc": {"max_hop": 2, "max_items": 1}},
    },
    "ranking": {"rank_by": ["hop", "kind", "edge", "same_file", "size"],
                "kind_order": ["changed", "callee", "caller", "test", "doc"],
                "edge_order": ["CALLS", "USAGE", "TESTS", "MENTIONS"]},
    "packing": {"token_budget": 10_000, "tokenizer": "approx",
                "kind_shares": {"changed": None, "callee": None, "caller": None, "test": None, "doc": None},
                "redistribute_unused": True, "max_lines_per_item": {"default": 50},
                "oversize": {"changed": "hunks", "default": "head"}, "hunk_context_lines": 1,
                "include_decorators": True, "extend_doc_sections": True, "line_numbers": False},
}


def config(**packing):
    cfg = copy.deepcopy(CONFIG)
    cfg["packing"].update(packing)
    return cfg


def chars(text):
    # Character count as a deterministic stand-in for a tokenizer.
    return len(text)


APP = "\n".join([
    "import functools",            # 1
    "",                            # 2
    "@functools.lru_cache",        # 3
    "def helper(x):",              # 4
    "    return x + 1",            # 5
    "",                            # 6
    "def changed(x):",             # 7
    "    y = helper(x)",           # 8
    "    return y",                # 9
    "",                            # 10
    "def caller():",               # 11
    "    return changed(2)",       # 12
])
TEST = "\n".join(["from app import changed", "", "def test_changed():", "    assert changed(1) == 2"])
DOC = "\n".join(["# Guide", "", "## Changed", "Uses `changed`.", "", "### Detail", "More.", "", "## Other", "x"])


def trace():
    return {
        "tool_version": "codebase-memory-mcp 0.12.1", "repo": "/repo", "base": "base",
        "head": "h" * 40, "merge_base": "m" * 40,
        "changed_files": ["app.py"], "hunks": {"app.py": [[8, 8]]},
        "seeds": [{"qn": "app.changed", "label": "Function", "file": "app.py", "start": 7, "end": 9}],
        "callers": [
            {"qn": "app.caller", "label": "Function", "file": "app.py", "hop": 1,
             "via_qn": "app.changed", "via_edge": "CALLS"},
            {"qn": "tests.test_app.test_changed", "label": "Function", "file": "tests/test_app.py", "hop": 1,
             "via_qn": "app.changed", "via_edge": "TESTS"},
            {"qn": "app", "label": "Module", "file": "app.py", "hop": 1, "via_qn": "app.changed",
             "via_edge": "DEFINES"},
            {"qn": "docs.guide.Changed", "label": "Section", "file": "docs/guide.md", "hop": 2,
             "via_qn": "app.caller", "via_edge": "MENTIONS"},
        ],
        "callees": [
            {"qn": "app.helper", "label": "Function", "file": "app.py", "hop": 1},
            {"qn": "builtins.len", "label": "Function", "file": "", "hop": 1},
        ],
        "nodes": {
            "app.changed": {"label": "Function", "file": "app.py", "start": 7, "end": 9},
            "app.caller": {"label": "Function", "file": "app.py", "start": 11, "end": 12},
            "app.helper": {"label": "Function", "file": "app.py", "start": 4, "end": 5},
            "tests.test_app.test_changed": {"label": "Function", "file": "tests/test_app.py", "start": 3, "end": 4},
            "docs.guide.Changed": {"label": "Section", "file": "docs/guide.md", "start": 3, "end": 3},
        },
        "callee_edges": [["app.changed", "app.helper"]],
        "warnings": [],
    }


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        for path, text in {"app.py": APP, "tests/test_app.py": TEST, "docs/guide.md": DOC}.items():
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / path).write_text(text + "\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()


class ConfigTests(unittest.TestCase):
    def test_valid_config_passes(self):
        self.assertIs(validate_config(copy.deepcopy(CONFIG))["version"], 1)

    def test_typos_and_bad_values_are_rejected(self):
        bad = []
        cfg = copy.deepcopy(CONFIG); cfg["packing"]["token_budgt"] = 1; bad.append(cfg)  # noqa: E702
        cfg = copy.deepcopy(CONFIG); del cfg["ranking"]["edge_order"]; bad.append(cfg)  # noqa: E702
        cfg = copy.deepcopy(CONFIG); cfg["tracer"]["index_mode"] = "deep"; bad.append(cfg)  # noqa: E702
        cfg = copy.deepcopy(CONFIG); cfg["tracer"]["workers"] = True; bad.append(cfg)  # noqa: E702
        cfg = copy.deepcopy(CONFIG); cfg["ranking"]["rank_by"] = ["hop", "speed"]; bad.append(cfg)  # noqa: E702
        cfg = copy.deepcopy(CONFIG); cfg["packing"]["kind_shares"] = {"changed": 0.7, "callee": 0.5}; bad.append(cfg)  # noqa: E501,E702
        cfg = copy.deepcopy(CONFIG); cfg["selection"]["limits"]["test"]["max_items"] = -1; bad.append(cfg)  # noqa: E702
        cfg = copy.deepcopy(CONFIG); cfg["packing"]["max_lines_per_item"] = {"changed": 5}; bad.append(cfg)  # noqa: E702
        for cfg in bad:
            with self.assertRaises(ValueError):
                validate_config(cfg)


class ParsingTests(unittest.TestCase):
    def test_test_globs_respect_path_parts(self):
        globs = CONFIG["selection"]["test_globs"]
        self.assertTrue(is_test_path("tests/unit/a.py", globs))
        self.assertTrue(is_test_path("pkg/sub/test_a.py", globs))
        self.assertTrue(is_test_path("test_a.py", globs))
        self.assertFalse(is_test_path("test_utils/a.py", globs))
        self.assertFalse(is_test_path("src/app.py", globs))

    def test_parse_hunks_new_side_ranges(self):
        diff = "\n".join([
            "diff --git a/a.py b/a.py", "--- a/a.py", "+++ b/a.py",
            "@@ -3 +3 @@", "-x", "+y", "@@ -10,2 +10,3 @@", "@@ -20,4 +22,0 @@",
            "diff --git a/old.py b/old.py", "--- a/old.py", "+++ /dev/null", "@@ -1,5 +0,0 @@",
        ])
        self.assertEqual(parse_hunks(diff), {"a.py": [[3, 3], [10, 12], [22, 22]]})

    def test_decorators_and_annotations_are_included(self):
        source = "import x\n\n@a\n@b(\n    1,\n)\ndef f():\n    pass\n"
        starts = python_decorator_starts(source)
        self.assertEqual(starts, {7: 3})
        self.assertEqual(annotation_start(source.splitlines(), 7, "m.py", starts), 3)
        java = ["class A {", "  @Override", "  @Deprecated", "  void f() {}", "}"]
        self.assertEqual(annotation_start(java, 4, "A.java"), 2)
        rust = ["#[test]", "fn t() {}"]
        self.assertEqual(annotation_start(rust, 2, "lib.rs"), 1)
        self.assertEqual(annotation_start(["x", "def g():"], 2, "unknown.go"), 2)

    def test_markdown_section_stops_at_same_or_higher_heading(self):
        lines = DOC.splitlines()
        self.assertEqual(markdown_section_end(lines, 3), 7)   # "## Changed" keeps its "###" child
        self.assertEqual(markdown_section_end(lines, 1), 10)  # top heading runs to the end
        fenced = ["## A", "```", "# not a heading", "```", "text", "## B"]
        self.assertEqual(markdown_section_end(fenced, 1), 5)
        self.assertIsNone(markdown_section_end(lines, 2))

    def test_oversize_policies(self):
        packing = dict(CONFIG["packing"], max_lines_per_item={"changed": 10, "default": 10}, hunk_context_lines=1)
        self.assertEqual(choose_segments(1, 8, "caller", [], packing), [(1, 8)])
        self.assertEqual(choose_segments(1, 30, "caller", [], packing), [(1, 10)])
        self.assertEqual(choose_segments(1, 30, "changed", [[15, 15], [17, 17], [40, 41]], packing),
                         [(1, 2), (14, 18)])
        self.assertEqual(choose_segments(1, 30, "changed", [[3, 25]], packing), [(1, 10)])
        packing["oversize"] = {"default": "skip"}
        self.assertIsNone(choose_segments(1, 30, "caller", [], packing))


class SelectionTests(unittest.TestCase):
    def test_collect_types_filters_and_merges(self):
        data = trace()
        data["seeds"].append({"qn": "app.Outer", "label": "Class", "file": "app.py", "start": 1, "end": 12})
        data["callers"].append({"qn": "app.helper", "label": "Function", "file": "app.py", "hop": 2,
                                "via_qn": "app.caller", "via_edge": "CALLS"})
        items, excluded = collect_items(data, CONFIG["selection"])
        kinds = {it.qn: (it.kind, it.hop, it.via, it.relations) for it in items}
        self.assertEqual(kinds["app.changed"][0], "changed")
        self.assertNotIn("app.Outer", kinds)                       # contains a changed function
        self.assertEqual(kinds["app.helper"], ("callee", 1, "app.changed", ("callee", "caller")))
        self.assertEqual(kinds["tests.test_app.test_changed"][0], "test")
        self.assertEqual(kinds["docs.guide.Changed"][0], "doc")
        reasons = {e["qn"]: e["reason"] for e in excluded}
        self.assertEqual(reasons, {"app.Outer": "contains_changed", "app": "label:Module",
                                   "builtins.len": "builtin"})

    def test_rank_and_limits_are_deterministic(self):
        items = [Item(f"m.f{i}", "Function", "m.py", i * 10, i * 10 + i, "caller", 1 + i % 2) for i in range(6)]
        limits = copy.deepcopy(CONFIG["selection"]["limits"])
        limits["caller"] = {"max_hop": 1, "max_items": 2}
        kept, excluded = apply_limits(list(reversed(items)), limits, CONFIG["ranking"], set())
        self.assertEqual([it.qn for it in kept], ["m.f0", "m.f2"])
        self.assertEqual(sorted(e["reason"] for e in excluded), ["max_hop"] * 3 + ["max_items"])
        key = rank_key(items[0], CONFIG["ranking"], {"m.py"})
        self.assertEqual(key[-3:], ("m.py", 0, "m.f0"))


class BuildContextTests(RepoTestCase):
    def test_full_context_in_rank_order_with_fixed_ranges(self):
        text, report = build_context(trace(), self.repo, config(), chars, "chars")
        headers = [line for line in text.splitlines()
                   if line.startswith(("### changed", "### callee", "### caller", "### test", "### doc"))]
        self.assertEqual([h.split(":")[0] for h in headers],
                         ["### changed", "### callee (hop 1)", "### caller (hop 1)", "### test (hop 1)",
                          "### doc (hop 2)"])
        self.assertIn("@functools.lru_cache\ndef helper(x):", text)      # decorator above the range
        self.assertIn("### Detail\nMore.", text)                         # section past its heading
        self.assertIn("called by app.changed", text)
        self.assertEqual(report["context_tokens"], len(text))
        self.assertLessEqual(report["context_tokens"], report["token_budget"])

    def test_budget_shares_and_second_pass(self):
        full, _ = build_context(trace(), self.repo, config(), chars, "chars")
        budget = len(full) // 2
        shares = {"changed": 0.5, "callee": 0.0, "caller": 0.0, "test": 0.0, "doc": 0.0}
        _, capped = build_context(trace(), self.repo, config(token_budget=budget, kind_shares=shares,
                                                             redistribute_unused=False), chars, "chars")
        self.assertEqual([i["kind"] for i in capped["included"]], ["changed"])
        _, refilled = build_context(trace(), self.repo, config(token_budget=budget, kind_shares=shares), chars,
                                    "chars")
        self.assertGreater(len(refilled["included"]), 1)
        self.assertEqual({i["pass"] for i in refilled["included"][1:]}, {2})
        self.assertLessEqual(refilled["context_tokens"], budget)
        _, empty = build_context(trace(), self.repo, config(token_budget=0), chars, "chars")
        self.assertEqual((empty["included"], empty["context_tokens"]), ([], 0))

    def test_same_input_gives_identical_output(self):
        first = build_context(trace(), self.repo, config(), chars, "chars")
        reordered = trace()
        reordered["callers"].reverse()
        reordered["nodes"] = dict(reversed(list(reordered["nodes"].items())))
        self.assertEqual(first, build_context(reordered, self.repo, config(), chars, "chars"))

    def test_lines_already_included_are_not_repeated(self):
        data = trace()
        data["callees"].append({"qn": "app.Block", "label": "Class", "file": "app.py", "hop": 2})
        data["nodes"]["app.Block"] = {"label": "Class", "file": "app.py", "start": 1, "end": 12}
        data["callee_edges"].append(["app.helper", "app.Block"])
        text, report = build_context(data, self.repo, config(), chars, "chars")
        skipped = [(e["qn"], e["by"]) for e in report["excluded"] if e["reason"] == "overlaps_included"]
        self.assertEqual(skipped, [("app.Block", "app.changed")])  # its lines are partly shown already
        self.assertEqual(text.count("def caller():"), 1)


if __name__ == "__main__":
    unittest.main()
