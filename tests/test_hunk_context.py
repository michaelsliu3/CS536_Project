"""Per-hunk context levels: splitting a PR's diff into hunks, repeating its trace from one hunk,
the "other changes" section, and the per-hunk dataset record."""
import copy
import tempfile
import unittest
from pathlib import Path

from src.tracer_context.build import (
    CONTEXT_HEADING, OTHER_HEADING, build_context, nesting_check, split_input, validate_config,
)
from src.tracer_context.dataset import FORMAT, FORMAT_HUNK, build_key, check_unit, hunk_units, pack_hunks
from src.tracer_context.hunks import HunkTracer, hunk_id, hunk_seeds, restrict_trace, split_diff
from src.tracer_context.runtime import load_level_configs

PROJECT_ROOT = Path(__file__).resolve().parents[1]

APP = "\n".join([
    "import os",                 # 1
    "",                          # 2
    "def helper(x):",            # 3
    "    return x + 1",          # 4
    "",                          # 5
    "def first(x):",             # 6
    "    y = helper(x)",         # 7
    "    return y",              # 8
    "",                          # 9
    "def second(x):",            # 10
    "    return first(x) * 2",   # 11
    "",                          # 12
    "def lonely():",             # 13
    "    return 0",              # 14
])
UTIL = "\n".join(["import json", "", "def dump(x):", "    return json.dumps(x)", "", "def load(s):", "    return json.loads(s)"])
MAIN = "\n".join(["from util import dump", "", "def run():", "    return dump({})"])
TEST = "\n".join(["from app import second", "", "def test_second():", "    assert second(1) == 4"])

DIFF = "\n".join([
    "diff --git a/app.py b/app.py",
    "index 111..222 100644",
    "--- a/app.py",
    "+++ b/app.py",
    "@@ -4,7 +4,7 @@ def helper(x):",
    "     return x + 1",
    " ",
    " def first(x):",
    "-    y = helper(x) + 0",
    "+    y = helper(x)",
    "     return y",
    " ",
    " def second(x):",
    "@@ -12,3 +12,3 @@ def second(x):",
    " ",
    " def lonely():",
    "-    return 1",
    "+    return 0",
    "diff --git a/util.py b/util.py",
    "index 333..444 100644",
    "--- a/util.py",
    "+++ b/util.py",
    "@@ -1,3 +1,3 @@",
    "-import simplejson as json",
    "+import json",
    " ",
    " def dump(x):",
    "",
])


def trace():
    """A PR that changes `first` and `lonely` in app.py and an import line of util.py."""
    return {
        "tool_version": "codebase-memory-mcp 0.12.1", "repo": "/repo", "base": "base", "head": "h" * 40,
        "merge_base": "m" * 40, "index": {},
        "tracer_settings": {"workers": 4, "index_mode": "full", "callees": {"depth": 2},
                            "callers": {"depth": 2, "edge_types": ["CALLS", "USAGE", "TESTS"]}},
        "changed_files": ["app.py", "util.py"],
        "hunks": {"app.py": [[7, 7], [14, 14]], "util.py": [[1, 1]]},
        "diff": DIFF,
        "guide_files": [],
        "seeds": [
            {"qn": "app.first", "label": "Function", "file": "app.py", "start": 6, "end": 8, "seed_reason": "hunk"},
            {"qn": "app.lonely", "label": "Function", "file": "app.py", "start": 13, "end": 14, "seed_reason": "hunk"},
            # no change in util.py touches a symbol, so the tool takes every symbol of the file
            {"qn": "util.dump", "label": "Function", "file": "util.py", "start": 3, "end": 4, "seed_reason": "whole_file"},
            {"qn": "util.load", "label": "Function", "file": "util.py", "start": 6, "end": 7, "seed_reason": "whole_file"},
        ],
        "callees": [{"qn": "app.helper", "label": "Function", "file": "app.py", "hop": 1}],
        "callers": [
            {"qn": "app.second", "label": "Function", "file": "app.py", "hop": 1, "via_qn": "app.first", "via_edge": "CALLS"},
            {"qn": "main.run", "label": "Function", "file": "main.py", "hop": 1, "via_qn": "util.dump", "via_edge": "CALLS"},
            {"qn": "tests.test_app.test_second", "label": "Function", "file": "tests/test_app.py", "hop": 2,
             "via_qn": "app.second", "via_edge": "TESTS"},
        ],
        "nodes": {
            "app.first": {"label": "Function", "file": "app.py", "start": 6, "end": 8},
            "app.lonely": {"label": "Function", "file": "app.py", "start": 13, "end": 14},
            "app.helper": {"label": "Function", "file": "app.py", "start": 3, "end": 4},
            "app.second": {"label": "Function", "file": "app.py", "start": 10, "end": 11},
            "util.dump": {"label": "Function", "file": "util.py", "start": 3, "end": 4},
            "util.load": {"label": "Function", "file": "util.py", "start": 6, "end": 7},
            "main.run": {"label": "Function", "file": "main.py", "start": 3, "end": 4},
            "tests.test_app.test_second": {"label": "Function", "file": "tests/test_app.py", "start": 3, "end": 4},
        },
        "callee_edges": [["app.first", "app.helper"]],
        "call_edges": [["app.first", "app.helper"]],
        "caller_edges": [["app.second", "CALLS", "app.first"], ["app.second", "USAGE", "app.lonely"],
                         ["main.run", "CALLS", "util.dump"], ["tests.test_app.test_second", "TESTS", "app.second"]],
        "text_hits": {"first": {"total": 1, "seeds": ["app.first"], "hits": [["app.py", 11]]},
                      "dump": {"total": 2, "seeds": ["util.dump"], "hits": [["main.py", 1], ["main.py", 4]]}},
        "warnings": [],
    }


def chars(text):
    return len(text)  # a deterministic stand-in for a tokenizer


def hunk_configs():
    """The real per-hunk level configs, counted in characters."""
    configs = load_level_configs(sorted((PROJECT_ROOT / "configs" / "levels_hunk").glob("*.yaml")))
    for cfg in configs.values():
        cfg["packing"]["tokenizer"] = "approx"
    return configs


class RepoTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self._tmp.name)
        for path, text in {"app.py": APP, "util.py": UTIL, "main.py": MAIN, "tests/test_app.py": TEST}.items():
            (self.repo / path).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / path).write_text(text + "\n", encoding="utf-8")

    def tearDown(self):
        self._tmp.cleanup()


class SplitDiffTests(unittest.TestCase):
    def test_hunks_keep_their_file_header_and_line_numbers(self):
        hunks = split_diff(DIFF)
        self.assertEqual([(h.index, h.file, h.new_start, h.new_end, h.old_start, h.old_end) for h in hunks],
                         [(1, "app.py", 4, 10, 4, 10), (2, "app.py", 12, 14, 12, 14), (3, "util.py", 1, 3, 1, 3)])
        first, second, third = hunks
        self.assertEqual(first.file_header, "diff --git a/app.py b/app.py\nindex 111..222 100644\n--- a/app.py\n+++ b/app.py")
        self.assertEqual(second.file_header, first.file_header)  # each hunk is a valid diff on its own
        self.assertTrue(second.text.endswith("@@ -12,3 +12,3 @@ def second(x):\n \n def lonely():\n-    return 1\n+    return 0"))
        self.assertEqual(third.header, "@@ -1,3 +1,3 @@")
        # The file headers and bodies put the diff back together.
        rebuilt = "\n".join([first.file_header, first.body, second.body, third.file_header, third.body])
        self.assertEqual(rebuilt, DIFF.rstrip("\n"))
        self.assertEqual(hunk_id("o__r__1", 3), "o__r__1__h0003")

    def test_new_deleted_renamed_and_binary_files_and_misleading_lines(self):
        diff = "\n".join([
            "diff --git a/new.sql b/new.sql", "new file mode 100644", "index 000..111", "--- /dev/null", "+++ b/new.sql",
            "@@ -0,0 +1,2 @@", "+-- a comment", "+select 1",
            "diff --git a/old.py b/old.py", "deleted file mode 100644", "index 111..000", "--- a/old.py", "+++ /dev/null",
            "@@ -1,2 +0,0 @@", "-x = 1", "-y = 2",
            "diff --git a/a.sql b/b.sql", "similarity index 80%", "rename from a.sql", "rename to b.sql",
            "--- a/a.sql", "+++ b/b.sql",
            "@@ -1,3 +1,3 @@", " select 1", "--- a removed SQL comment", "+++ an added line", " select 2",
            "\\ No newline at end of file",
            "@@ -9 +9 @@", "-old", "+new",
            "diff --git a/logo.png b/logo.png", "index 111..222 100644", "Binary files a/logo.png and b/logo.png differ",
            "diff --git a/run.sh b/run.sh", "old mode 100644", "new mode 100755",
        ])
        hunks = split_diff(diff)
        self.assertEqual([(h.file, h.old_file, h.deleted_file, h.new_start, h.new_count) for h in hunks],
                         [("new.sql", None, False, 1, 2), ("old.py", None, True, 0, 0),
                          ("b.sql", "a.sql", False, 1, 3), ("b.sql", "a.sql", False, 9, 1)])
        # A removed line that starts with "-- " and an added "++ " line stay inside their hunk.
        self.assertEqual(hunks[2].body.split("\n")[2:4], ["--- a removed SQL comment", "+++ an added line"])
        self.assertTrue(hunks[2].body.endswith("\\ No newline at end of file"))
        self.assertEqual((hunks[3].old_count, hunks[3].new_end), (1, 9))  # a header without counts means one line
        self.assertEqual(split_diff(""), [])


class RestrictTraceTests(unittest.TestCase):
    def setUp(self):
        self.data = trace()
        self.tracer = HunkTracer(self.data)

    def test_each_hunk_starts_from_its_own_changed_symbols(self):
        first, lonely, imports = (self.tracer.restrict(h) for h in self.tracer.hunks)
        self.assertEqual([s["qn"] for s in first["seeds"]], ["app.first"])
        self.assertEqual([(r["qn"], r["hop"]) for r in first["callees"]], [("app.helper", 1)])
        self.assertEqual([(r["qn"], r["hop"], r["via_qn"], r["via_edge"]) for r in first["callers"]],
                         [("app.second", 1, "app.first", "CALLS"),
                          ("tests.test_app.test_second", 2, "app.second", "TESTS")])
        self.assertEqual(first["callee_edges"], [["app.first", "app.helper"]])
        self.assertEqual((list(first["text_hits"]), first["changed_files"], first["hunks"]),
                         (["first"], ["app.py"], {"app.py": [[7, 7]]}))
        self.assertEqual(first["diff"], self.tracer.hunks[0].text)
        self.assertEqual([o["index"] for o in first["other_hunks"]], [2, 3])  # the same file first

        # `lonely` calls nothing, and is used by `second` through another kind of link.
        self.assertEqual([s["qn"] for s in lonely["seeds"]], ["app.lonely"])
        self.assertEqual(lonely["callees"], [])
        self.assertEqual([(r["qn"], r["hop"], r["via_edge"]) for r in lonely["callers"]],
                         [("app.second", 1, "USAGE"), ("tests.test_app.test_second", 2, "TESTS")])
        self.assertEqual((lonely["text_hits"], lonely["hunks"]), ({}, {"app.py": [[14, 14]]}))
        self.assertEqual([o["index"] for o in lonely["other_hunks"]], [1, 3])

        # The import line is inside no symbol, so the hunk gets the file's whole_file symbols.
        self.assertEqual([s["qn"] for s in imports["seeds"]], ["util.dump", "util.load"])
        self.assertEqual([(r["qn"], r["hop"]) for r in imports["callers"]], [("main.run", 1)])
        self.assertEqual((list(imports["text_hits"]), imports["changed_files"]), (["dump"], ["util.py"]))
        self.assertEqual(self.data["seeds"], trace()["seeds"])  # the PR's trace is not changed

    def test_another_changed_symbol_can_be_a_hunks_caller(self):
        data = trace()
        data["caller_edges"].append(["app.lonely", "CALLS", "app.first"])  # one changed function calls the other
        first = restrict_trace(data, split_diff(DIFF)[0])
        self.assertIn(("app.lonely", 1, "CALLS"), [(r["qn"], r["hop"], r["via_edge"]) for r in first["callers"]])
        self.assertEqual({r["qn"]: r["label"] for r in first["callers"]}["app.lonely"], "Function")

    def test_walks_over_the_kept_links_repeat_the_tools_walk(self):
        self.assertEqual(self.tracer.check_links(), {
            "callees": {"tool": 1, "walked": 1, "not_walked": 0, "only_walked": 0, "hop_differs": 0},
            "callers": {"tool": 3, "walked": 3, "not_walked": 0, "only_walked": 0, "hop_differs": 0}})
        data = trace()
        data["caller_edges"] = data["caller_edges"][:1]  # links the tool followed are missing
        self.assertEqual(HunkTracer(data).check_links()["callers"]["not_walked"], 2)

    def test_a_trace_without_links_is_refused_and_edge_cases(self):
        data = trace()
        del data["caller_edges"]
        with self.assertRaisesRegex(ValueError, "per_seed_edges"):
            HunkTracer(data)
        deleted = split_diff("diff --git a/gone.py b/gone.py\n--- a/gone.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1")[0]
        sub = restrict_trace(trace(), deleted, [deleted])
        self.assertEqual((sub["seeds"], sub["changed_files"], sub["hunks"], sub["other_hunks"]), ([], [], {}, []))
        self.assertEqual(hunk_seeds(trace(), deleted), [])


class OtherHunksSectionTests(RepoTestCase):
    def setUp(self):
        super().setUp()
        self.configs = hunk_configs()
        self.sub = HunkTracer(trace()).restrict(split_diff(DIFF)[0])
        self.pr = {"title": "Tidy", "body": "", "edited_after_review": False}

    def build(self, name, parent_report=None, **packing):
        cfg = copy.deepcopy(self.configs[name])
        cfg["packing"].update(packing)
        return build_context(self.sub, self.repo, cfg, chars, "chars", pr=self.pr, parent_report=parent_report)

    def test_section_sits_between_the_hunk_and_the_repository_context(self):
        reports, texts = {}, {}
        for name in ("H01_min", "H02_function", "H03_file", "H04_pr_diff", "H05_callees1"):
            parent = self.configs[name]["level"]["parent"]
            texts[name], reports[name] = self.build(name, reports.get(parent))
        self.assertNotIn(OTHER_HEADING, texts["H03_file"])
        self.assertIsNone(reports["H03_file"]["other_hunks"])
        text = texts["H04_pr_diff"]
        self.assertLess(text.index("## Diff"), text.index(OTHER_HEADING))
        self.assertLess(text.index(OTHER_HEADING), text.index(CONTEXT_HEADING))
        section = text[text.index(OTHER_HEADING):text.index(CONTEXT_HEADING)]
        self.assertIn("@@ -12,3 +12,3 @@ def second(x):", section)  # the other hunk of app.py
        self.assertIn("+++ b/util.py\n@@ -1,3 +1,3 @@", section)   # and util.py's, under its own header
        self.assertNotIn("@@ -4,7 +4,7 @@", section)               # the hunk under review is not repeated
        self.assertEqual((reports["H04_pr_diff"]["other_hunks"]["total"], reports["H04_pr_diff"]["other_hunks"]["shown"]), (2, 2))
        self.assertTrue(texts["H04_pr_diff"].startswith(texts["H01_min"]))  # PR text and hunk come first, unchanged
        # Every later level shows the same section, and the levels stay nested.
        self.assertIn(section, texts["H05_callees1"])
        self.assertTrue(nesting_check(reports["H03_file"], reports["H04_pr_diff"])["ok"])
        self.assertTrue(nesting_check(reports["H04_pr_diff"], reports["H05_callees1"])["ok"])

    def test_room_is_filled_nearest_first_and_a_cut_is_said(self):
        reports = {}
        for name in ("H01_min", "H02_function", "H03_file"):
            _, reports[name] = self.build(name, reports.get(self.configs[name]["level"]["parent"]))
        near = self.sub["other_hunks"][0]
        room = chars(f"{near['file_header']}\n{near['body']}\n")
        text, report = self.build("H04_pr_diff", reports["H03_file"], other_hunks_max_tokens=room)
        self.assertEqual((report["other_hunks"]["total"], report["other_hunks"]["shown"]), (2, 1))
        self.assertIn("(1 of the 2 other changes are shown, the nearest ones.)", text)
        self.assertNotIn("util.py", text[text.index(OTHER_HEADING):text.index(CONTEXT_HEADING)])
        text, report = self.build("H04_pr_diff", reports["H03_file"], other_hunks_max_tokens=0)
        self.assertNotIn(OTHER_HEADING, text)
        self.assertEqual(report["other_hunks"], {"total": 2, "shown": 0, "tokens": 0})
        # A level that showed fewer of them than its parent is not nested.
        _, full = self.build("H04_pr_diff", reports["H03_file"])
        self.assertFalse(nesting_check(full, report)["other_hunks_kept"])

    def test_new_config_keys_are_optional_and_checked(self):
        per_pr = load_level_configs(sorted((PROJECT_ROOT / "configs" / "levels").glob("*.yaml")))
        self.assertTrue(all("other_hunks" not in cfg["input"] for cfg in per_pr.values()))  # unchanged, as are their hashes
        cfg = copy.deepcopy(self.configs["H04_pr_diff"])
        self.assertEqual((cfg["input"]["other_hunks"], cfg["packing"]["other_hunks_max_tokens"]), (True, 16000))
        for section, key, bad in (("input", "other_hunks", "yes"), ("packing", "other_hunks_max_tokens", -1),
                                  ("input", "other_hunk", True)):
            broken = copy.deepcopy(cfg)
            broken[section][key] = bad
            with self.assertRaises(ValueError):
                validate_config(broken)


class HunkDatasetTests(RepoTestCase):
    def test_every_hunk_gets_its_own_levels(self):
        configs = hunk_configs()
        self.assertEqual(list(configs), ["H01_min", "H02_function", "H03_file", "H04_pr_diff", "H05_callees1",
                                         "H06_callers1", "H07_tests1", "H08_agent", "H09_depth2", "H10_depth2_docs",
                                         "H11_all"])
        pr = {"title": "Tidy", "body": "", "edited_after_review": False}
        hunks, checks = pack_hunks(trace(), self.repo, configs, {"approx": (chars, "chars")}, pr, "o__r__1")
        self.assertEqual([h["id"] for h in hunks], ["o__r__1__h0001", "o__r__1__h0002", "o__r__1__h0003"])
        self.assertEqual(checks["hunks"], 3)
        self.assertEqual((checks["hunks_with_changed_symbols"], checks["changed_symbols"],
                          checks["changed_symbols_in_no_hunk"]), (3, 4, 0))
        self.assertEqual(checks["links"]["callers"]["not_walked"], 0)
        first, lonely, imports = hunks
        self.assertEqual((first["file"], first["new_start"], first["new_count"], first["stats"]["changed_symbols"],
                          first["stats"]["added_lines"], first["stats"]["removed_lines"]), ("app.py", 4, 7, 1, 1, 1))
        self.assertEqual(imports["stats"]["callers_by_hop"], {"1": 1})

        text = first["levels"]["H01_min"]["text"]
        self.assertTrue(text.startswith("## Pull request\n\nTitle: Tidy"))
        self.assertIn("@@ -4,7 +4,7 @@ def helper(x):", text)
        self.assertNotIn("def lonely", text)  # only this hunk
        self.assertIn("### changed: app.first", first["levels"]["H02_function"]["text"])
        self.assertIn("### changed file: app.py", first["levels"]["H03_file"]["text"])
        self.assertEqual((first["levels"]["H04_pr_diff"]["other_hunks_total"], first["levels"]["H04_pr_diff"]["other_hunks_shown"]), (2, 2))
        # app.py is shown whole from H03 on, so `helper` and `second` add nothing: no second copy of the text.
        self.assertTrue(first["levels"]["H05_callees1"]["same_as_parent"])
        self.assertIsNone(first["levels"]["H05_callees1"]["text"])
        # The import hunk's caller is in another file and appears at the callers level.
        self.assertNotIn("main.run", imports["levels"]["H05_callees1"]["text"] or imports["levels"]["H04_pr_diff"]["text"])
        self.assertIn("### caller (hop 1): main.run", imports["levels"]["H06_callers1"]["text"])
        self.assertTrue(all(level["nesting_ok"] is not False and level["fits"]
                            for hunk in hunks for level in hunk["levels"].values()))

        # Every level records where its parts are, so a prompt with a slot for each can be filled.
        self.assertEqual(sorted(first["levels"]["H01_min"]["sections"]), ["diff", "pr"])
        level = imports["levels"]["H06_callers1"]
        self.assertEqual(sorted(level["sections"]), ["context", "diff", "other", "pr"])
        parts = split_input(level["text"], level["sections"])
        self.assertEqual((parts["pr_title"], parts["pr_description"]), ("Tidy", "(no description)"))
        self.assertEqual(parts["diff"], "diff --git a/util.py b/util.py\nindex 333..444 100644\n--- a/util.py\n+++ b/util.py\n"
                                        "@@ -1,3 +1,3 @@\n-import simplejson as json\n+import json\n \n def dump(x):")
        self.assertTrue(parts["contexts"].startswith("### Other changes in this pull request\n\n```diff\ndiff --git a/app.py"))
        self.assertIn("### caller (hop 1): main.run", parts["contexts"])
        self.assertNotIn("## Repository context", parts["contexts"])  # the prompt has its own heading for it
        plain = split_input(first["levels"]["H01_min"]["text"], first["levels"]["H01_min"]["sections"])
        self.assertEqual(plain["contexts"], "")

        record = {"id": "o__r__1", "index": 4, "language": "Python", "known_paths": ["app.py"], "hunks": hunks}
        units = hunk_units([record])
        self.assertEqual([(u["id"], u["pr_id"], u["index"], u["hunk"]["file"]) for u in units],
                         [("o__r__1__h0001", "o__r__1", 4, "app.py"), ("o__r__1__h0002", "o__r__1", 4, "app.py"),
                          ("o__r__1__h0003", "o__r__1", 4, "util.py")])
        filled = units[0]["levels"]
        self.assertEqual(filled["H05_callees1"]["text"], filled["H04_pr_diff"]["text"])  # the parent's text, filled in
        self.assertTrue(all(isinstance(level["text"], str) for unit in units for level in unit["levels"].values()))
        self.assertNotIn("levels", units[0]["hunk"])

    def test_a_per_hunk_build_has_its_own_resume_key(self):
        configs = hunk_configs()
        record = {"githubPrUrl": "https://github.com/o/r/pull/1", "source_commit": "s", "target_commit": "t"}
        args = (configs, "tool 1", {"approx": "chars"}, record)
        self.assertNotEqual(build_key(*args), build_key(*args, unit="hunk"))
        self.assertEqual(build_key(*args), build_key(*args, unit="pr"))
        self.assertNotEqual(FORMAT, FORMAT_HUNK)

    def test_a_build_refuses_a_file_that_holds_the_other_units_records(self):
        per_pr = {"id": "o__r__1", "status": "ok", "levels": {}}
        per_hunk = {"id": "o__r__2", "status": "ok", "hunks": []}
        failed = {"id": "o__r__3", "status": "failed"}  # a failed record has neither; it is retried either way
        check_unit([per_pr, failed], "pr")
        check_unit([per_hunk, failed], "hunk")
        check_unit([], "hunk")
        with self.assertRaisesRegex(ValueError, "per-PR records"):
            check_unit([per_pr, failed], "hunk")  # a per-hunk build pointed at the per-PR file
        with self.assertRaisesRegex(ValueError, "per-hunk records"):
            check_unit([per_hunk], "pr")


if __name__ == "__main__":
    unittest.main()
