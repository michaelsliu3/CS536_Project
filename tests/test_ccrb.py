"""ContextCRBench records as AACR-shaped dataset entries (src/tracer_context/ccrb.py)."""
import importlib.util
import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.tracer_context import ccrb  # noqa: E402

_spec = importlib.util.spec_from_file_location("contextcrbench_cli", PROJECT_ROOT / "scripts/dataset/contextcrbench.py")
cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cli)

# The hunk GitHub returned for a comment on kubernetes/kubernetes PR 122234: cut at the commented line.
SEEN = ("@@ -167,3 +170,21 @@ func ScoreExtensions() {\n func New() {\n \treturn nil\n }\n+\n+// one\n+// two\n+func changed() {")


def record(**changes) -> dict:
    base = {"id": 7, "full_name": "o/r", "pr_number": 5, "lang": "c_sharp", "path": "a.cs", "line": 217, "side": "RIGHT",
            "review_comment": ["Please add tests.", "done"], "commit_id": "h" * 40, "original_commit_id": "b" * 40,
            "created_at": "2023-12-10T00:10:49Z", "merged": True, "pr_title": "T", "diff_hunk": "final", "content_after": "final"}
    return {**base, **changes}


def comment(**changes) -> dict:
    base = {"id": 1, "path": "a.cs", "body": "Please add  tests.\r\n", "diff_hunk": SEEN, "original_commit_id": "x" * 40,
            "side": "RIGHT", "start_side": None, "line": 217, "original_line": 217, "start_line": None, "original_start_line": None,
            "created_at": "2023-12-10T00:10:49Z", "in_reply_to_id": None, "author_association": "MEMBER",
            "user": {"login": "reviewer", "type": "User"}}
    return {**base, **changes}


class ThreadTests(unittest.TestCase):
    def test_repeated_records_become_one_thread_without_the_final_code(self):
        threads = ccrb.collect_threads([record(), record(id=8), record(id=9, line=3), record(id=10, review_comment=[" "])])
        self.assertEqual([t["ccrb_ids"] for t in threads], [[7, 8], [9]])
        first = threads[0]
        self.assertEqual((first["thread"], first["language"], first["side"]), ("o__r__5::7", "C#", "right"))
        self.assertEqual((first["base_commit"], first["head_commit"]), ("b" * 40, "h" * 40))
        self.assertEqual((first["comment"], first["replies"]), ("Please add tests.", ["done"]))
        self.assertFalse({"diff_hunk", "content_after"} & first.keys())


class LineTests(unittest.TestCase):
    def test_the_commented_line_is_the_last_line_of_the_hunk_the_reviewer_saw(self):
        self.assertEqual(ccrb.commented_line(SEEN, "right"), 176)  # the record itself says 217
        self.assertEqual(ccrb.commented_line(SEEN + "\n", "right"), 176)
        self.assertEqual(ccrb.commented_line("@@ -10,4 +10,2 @@\n a\n-b\n-c", "left"), 12)
        self.assertEqual(ccrb.commented_line("@@ -10,4 +10,2 @@\n a\n-b\n\\ No newline at end of file", "left"), 11)
        self.assertIsNone(ccrb.commented_line("not a hunk", "right"))
        self.assertIsNone(ccrb.commented_line("@@ -1,2 +0,0 @@\n-a\n-b", "right"))

    def test_a_reference_takes_its_lines_from_that_hunk(self):
        thread = ccrb.collect_threads([record()])[0]
        ref = ccrb.reference(thread, comment())
        self.assertEqual((ref["from_line"], ref["to_line"], ref["side"], ref["note"]), (176, 176, "right", "Please add tests."))
        self.assertEqual((ref["is_ai_comment"], ref["source_model"], ref["category"], ref["context"]), (False, "", None, None))
        spanning = ccrb.reference(thread, comment(original_start_line=214, original_line=217, start_side="RIGHT"))
        self.assertEqual((spanning["from_line"], spanning["to_line"]), (173, 176))
        self.assertIsNone(ccrb.reference(thread, comment(diff_hunk="")))


class MatchTests(unittest.TestCase):
    def test_a_thread_is_found_by_file_and_text(self):
        thread = ccrb.collect_threads([record()])[0]
        other_file, reply = comment(id=2, path="b.cs"), comment(id=3, in_reply_to_id=1, created_at="2024-01-01T00:00:00Z")
        self.assertEqual(ccrb.match_comment(thread, [other_file, reply, comment()])["id"], 1)
        self.assertIsNone(ccrb.match_comment(thread, [other_file, comment(body="something else")]))

    def test_the_commit_with_most_threads_is_kept_and_the_earliest_on_a_tie(self):
        thread = ccrb.collect_threads([record()])[0]
        on = lambda sha, when: (thread, comment(original_commit_id=sha, created_at=when))  # noqa: E731
        self.assertEqual(ccrb.choose_commit([on("a", "2"), on("b", "3"), on("b", "4")]), "b")
        self.assertEqual(ccrb.choose_commit([on("a", "2"), on("b", "1")]), "b")
        self.assertIsNone(ccrb.choose_commit([]))


class ConvertTests(unittest.TestCase):
    def found(self, comments, **pull):
        size = {"changed_files": 3, "additions": 10, "deletions": 2, "created_at": "c", "merged_at": None}
        return {"pull": {**size, **pull, "user": {"login": "author"}}, "comments": comments,
                "commits": {"x" * 40: {"exists": True}, "g" * 40: {"exists": False}}}

    def convert(self, records, found):
        return cli.convert_pr("o__r__5", ccrb.collect_threads(records), found, max_files=30, max_lines=2000)

    def test_an_entry_has_aacr_fields_and_the_reviewed_commit(self):
        made, rows, dropped = self.convert([record()], self.found([comment()]))
        self.assertEqual((made["githubPrUrl"], made["source_commit"], made["target_commit"], made["project_main_language"]),
                         ("https://github.com/o/r/pull/5", "b" * 40, "x" * 40, "C#"))
        self.assertEqual((len(made["comments"]), rows[0]["to_line"], rows[0]["final_line"], rows[0]["made_on_head"]), (1, 176, 217, False))
        self.assertFalse(dropped)

    def test_what_cannot_be_used_is_dropped_with_its_reason(self):
        records = [record(), record(id=8, line=1, review_comment=["gone"]), record(id=9, line=2, review_comment=["mine"]),
                   record(id=10, line=3, review_comment=["missing"])]
        comments = [comment(), comment(id=2, body="gone", original_commit_id="g" * 40),
                    comment(id=3, body="mine", user={"login": "author", "type": "User"})]
        made, _, dropped = self.convert(records, self.found(comments))
        self.assertEqual(len(made["comments"]), 1)
        self.assertEqual(dict(dropped), {"reviewed commit is gone": 1, "comment by the PR's author": 1, "comment not found on GitHub": 1})
        made, _, dropped = self.convert([record()], self.found([comment()], changed_files=31))
        self.assertIsNone(made)
        self.assertEqual(dict(dropped), {"PR over the size limit": 1})


class CheckTests(unittest.TestCase):
    def test_a_line_of_a_built_hunk_is_found_by_its_number(self):
        header, body = "@@ -10,3 +10,3 @@ f", [" a", "-b", "+c", " d"]
        self.assertEqual(cli.line_text(header, body, "right", 11), "c")
        self.assertEqual(cli.line_text(header, body, "left", 11), "b")
        self.assertEqual(cli.line_text(header, body, "right", 12), "d")
        self.assertIsNone(cli.line_text(header, body, "right", 13))

    def test_the_hunk_is_read_from_a_levels_diff_section(self):
        text = "## Pull request\n\nTitle: T\n\n## Diff\n\n```diff\ndiff --git a/a b/a\n--- a/a\n+++ b/a\n@@ -1,2 +1,2 @@ f\n a\n-b\n+c\n```"
        start = text.index("## Diff")
        self.assertEqual(cli.built_hunk(text, {"diff": [start, len(text)]}), ("@@ -1,2 +1,2 @@ f", [" a", "-b", "+c"]))


if __name__ == "__main__":
    unittest.main()
