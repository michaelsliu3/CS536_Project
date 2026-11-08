import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.tracer_context.aacr import (
    BASE_BRANCH, entry_checks, entry_id, load_entries, parse_pr_url, prepare_checkout, select_entries,
)

GIT_ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
           "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(cwd, *args):
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env=GIT_ENV).stdout.strip()


def commit(repo, path, text, message):
    (repo / path).write_text(text, encoding="utf-8")
    git(repo, "add", path)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def record(url="https://github.com/o/r/pull/7", language="Go", **extra):
    return {"githubPrUrl": url, "project_main_language": language, "source_commit": "s", "target_commit": "t",
            "change_line_count": 1, "comments": [], **extra}


class EntryTests(unittest.TestCase):
    def test_ids_come_from_the_pr_url(self):
        self.assertEqual(parse_pr_url("https://github.com/vllm-project/vllm/pull/17425"),
                         ("vllm-project", "vllm", 17425))
        self.assertEqual(entry_id(record("https://github.com/a/b/pull/3/")), "a__b__3")
        with self.assertRaises(ValueError):
            parse_pr_url("https://github.com/a/b/issues/3")

    def test_load_and_select(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "d.json"
            rows = [record("https://github.com/a/b/pull/1", "Go"), record("https://github.com/a/b/pull/2", "Java"),
                    record("https://github.com/c/d/pull/3", "go")]
            path.write_text(json.dumps(rows), encoding="utf-8")
            entries = load_entries(path)
            self.assertEqual([e["id"] for e in entries], ["a__b__1", "a__b__2", "c__d__3"])
            self.assertEqual([e["id"] for e in select_entries(entries, languages=["GO"])], ["a__b__1", "c__d__3"])
            # indices are 0-based positions in the file, as in positive_samples.json[111]
            self.assertEqual([e["id"] for e in select_entries(entries, ids=["1", "a__b__1"])], ["a__b__1", "a__b__2"])
            self.assertEqual(len(select_entries(entries, limit=1)), 1)
            with self.assertRaises(ValueError):
                select_entries(entries, ids=["nope"])
            path.write_text(json.dumps(rows + rows[:1]), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_entries(path)


class CheckoutTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.origin, self.repos = root / "origin", root / "repos"
        self.origin.mkdir()
        git(self.origin, "init", "-q", "-b", "main")
        git(self.origin, "config", "uploadpack.allowAnySHA1InWant", "true")
        lines = "".join(f"{i}\n" for i in range(1, 21))
        self.fork_point = commit(self.origin, "a.py", lines, "base")
        for i in range(6):  # main moves on, so a shallow fetch misses the fork point
            self.source = commit(self.origin, "main.txt", f"{i}\n", f"main {i}")
        git(self.origin, "checkout", "-q", "-b", "pr", self.fork_point)
        commit(self.origin, "a.py", lines + "21\n", "pr 1")
        self.target = commit(self.origin, "a.py", lines + "21\n22\n", "pr 2")  # diff shows lines 18-22
        git(self.origin, "checkout", "-q", "main")
        self.record = {
            "githubPrUrl": "https://github.com/o/r/pull/7", "source_commit": self.source,
            "target_commit": self.target, "change_line_count": 2,
            "comments": [{"path": "a.py", "side": "right", "from_line": 20, "to_line": 22},  # in the diff
                         {"path": "a.py", "side": "right", "from_line": 1, "to_line": 2},    # outside it
                         {"path": "a.py", "side": "left", "from_line": 1, "to_line": 3},
                         {"path": "a.py", "side": "right", "from_line": 21, "to_line": 99},
                         {"path": "gone.py", "side": "right", "from_line": 1, "to_line": 1}]}

    def tearDown(self):
        self._tmp.cleanup()

    def checkout(self):
        return prepare_checkout(self.repos, self.record, depth=1, max_depth=64, remote_url=f"file://{self.origin}")

    def test_fetches_both_commits_and_deepens_to_the_merge_base(self):
        info = self.checkout()
        repo = Path(info["repo"])
        self.assertEqual(info["merge_base"], self.fork_point)
        self.assertTrue(any(step.startswith("deepen") for step in info["fetch_log"]))
        self.assertEqual(git(repo, "rev-parse", "HEAD"), self.target)
        self.assertEqual(git(repo, "rev-parse", BASE_BRANCH), self.source)
        checks = entry_checks(repo, self.record, info["merge_base"])
        self.assertEqual(checks, {"diff_files": 1, "diff_lines": 2, "change_line_count": 2, "diff_lines_match": True,
                                  "comment_lines": {"ok": 3, "missing_file": 1, "out_of_range": 1,
                                                    "right_side_outside_diff": 1}})

    def test_reuses_the_clone_and_resets_a_dirty_worktree(self):
        repo = Path(self.checkout()["repo"])
        (repo / "a.py").write_text("edited\n", encoding="utf-8")
        (repo / "stray.txt").write_text("x\n", encoding="utf-8")
        info = self.checkout()
        self.assertEqual(info["fetch_log"], [])
        self.assertEqual(git(repo, "status", "--porcelain"), "")
        self.assertFalse((repo / "stray.txt").exists())

    def test_missing_commit_is_reported(self):
        self.record["target_commit"] = "0" * 40
        with self.assertRaisesRegex(Exception, "target_commit"):
            self.checkout()


if __name__ == "__main__":
    unittest.main()
