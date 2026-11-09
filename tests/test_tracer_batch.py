import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.tracer_context.aacr import (
    BASE_BRANCH, CommitGoneError, FetchError, entry_checks, entry_id, github_merge_base, load_entries,
    parse_pr_url, prepare_checkout, select_entries,
)
from src.tracer_context.cbm import text_hits

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

    def checkout(self, merge_base=None):
        merge_base = merge_base or git(self.origin, "merge-base", self.source, self.target)  # what GitHub reports
        return prepare_checkout(self.repos, self.record, merge_base, depth=1, max_depth=64,
                                remote_url=f"file://{self.origin}")

    def test_uses_the_merge_base_and_deepens_the_target_to_reach_it(self):
        info = self.checkout()
        repo = Path(info["repo"])
        self.assertEqual(info["merge_base"], self.fork_point)
        self.assertTrue(any(step.startswith("deepen") for step in info["fetch_log"]))
        self.assertEqual(git(repo, "rev-parse", "HEAD"), self.target)
        self.assertEqual(git(repo, "rev-parse", BASE_BRANCH), self.fork_point)
        self.assertEqual(git(repo, "merge-base", BASE_BRANCH, "HEAD"), self.fork_point)  # what the tracer computes
        source_fetched = subprocess.run(["git", "-C", str(repo), "cat-file", "-e", f"{self.source}^{{commit}}"],
                                        capture_output=True).returncode == 0
        self.assertFalse(source_fetched)  # main's newer history is never needed
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
        merge_base = git(self.origin, "merge-base", self.source, self.target)
        self.record["target_commit"] = "0" * 40
        with self.assertRaisesRegex(FetchError, "target_commit"):
            self.checkout(merge_base)

    def test_merge_base_the_target_cannot_reach_is_an_error(self):
        with self.assertRaisesRegex(FetchError, "not an ancestor"):
            self.checkout(merge_base=self.source)  # main's tip is not in the PR's history
        with self.assertRaisesRegex(FetchError, "not a commit id"):
            self.checkout(merge_base="main")

    def test_stale_common_ancestor_of_a_shallow_clone_is_not_used(self):
        """FreeCAD#20612: main merged a branch that forked before the PR did. A shallow clone with
        both tips then finds that older fork as their merge base, and the diff gains main's commits."""
        origin = Path(self._tmp.name) / "origin2"
        origin.mkdir()
        git(origin, "init", "-q", "-b", "main")
        git(origin, "config", "uploadpack.allowAnySHA1InWant", "true")
        old = commit(origin, "a.py", "a\n", "A")
        git(origin, "checkout", "-q", "-b", "side")
        commit(origin, "side.txt", "s\n", "side")
        git(origin, "checkout", "-q", "main")
        fork = commit(origin, "f.txt", "f\n", "F, where the PR forks")
        for i in range(5):
            commit(origin, "main.txt", f"{i}\n", f"main {i}")
        git(origin, "merge", "-q", "--no-ff", "-m", "merge side", "side")
        source = git(origin, "rev-parse", "HEAD")
        git(origin, "checkout", "-q", "-b", "pr", fork)
        commit(origin, "pr.txt", "1\n", "pr 1")
        target = commit(origin, "pr.txt", "1\n2\n", "pr 2")
        record = {"githubPrUrl": "https://github.com/o/r2/pull/1", "source_commit": source, "target_commit": target,
                  "change_line_count": 2, "comments": []}
        remote = f"file://{origin}"

        stale = Path(self._tmp.name) / "stale"  # the old way: both tips 4 commits deep, then git merge-base
        stale.mkdir()
        git(stale, "init", "-q")
        git(stale, "fetch", "-q", "--depth=4", remote, source, target)
        self.assertEqual(git(stale, "merge-base", source, target), old)

        info = prepare_checkout(self.repos, record, git(origin, "merge-base", source, target), depth=4,
                                max_depth=64, remote_url=remote)
        self.assertEqual(info["merge_base"], fork)
        checks = entry_checks(Path(info["repo"]), record, info["merge_base"])
        self.assertEqual((checks["diff_files"], checks["diff_lines"]), (1, 2))  # only pr.txt


class GithubMergeBaseTests(unittest.TestCase):
    def call(self, returncode, stdout, stderr=""):
        done = subprocess.CompletedProcess([], returncode, stdout, stderr)
        with mock.patch("src.tracer_context.aacr.subprocess.run", return_value=done) as run:
            return github_merge_base("https://github.com/o/r/pull/7", "a" * 40, "b" * 40), run

    def test_reads_the_compare_api(self):
        sha, run = self.call(0, "c" * 40 + "\n")
        self.assertEqual(sha, "c" * 40)
        self.assertIn(f"repos/o/r/compare/{'a' * 40}...{'b' * 40}?per_page=1", run.call_args.args[0])

    def test_failures_are_fetch_errors(self):
        with self.assertRaisesRegex(CommitGoneError, "force-pushed away"):
            self.call(1, "", "gh: Not Found (HTTP 404)")
        with self.assertRaisesRegex(FetchError, "failed: gh: Bad Gateway") as caught:
            self.call(1, "", "gh: Bad Gateway (HTTP 502)")
        self.assertNotIsInstance(caught.exception, CommitGoneError)  # may work on a retry
        with self.assertRaisesRegex(FetchError, "no merge base"):
            self.call(0, "null\n")


class TextHitsTests(unittest.TestCase):
    def test_whole_words_lines_binaries_and_untracked(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            git(repo, "init", "-q")
            (repo / "a.py").write_text("x = warn_once(1)\nwarn_once_all()\n# warn_once warn_once\r\n1warn_once\n",
                                       encoding="utf-8")
            (repo / "b.bin").write_bytes(b"\0warn_once\n")
            (repo / "c.md").write_text("Call `warn_once` or Size.\n", encoding="utf-8")
            git(repo, "add", "a.py", "b.bin", "c.md")
            git(repo, "commit", "-q", "-m", "files")
            (repo / "untracked.py").write_text("warn_once\n", encoding="utf-8")
            seeds = [{"qn": "pkg.log.warn_once", "label": "Function"}, {"qn": "pkg.Size", "label": "Method"},
                     {"qn": "pkg.mod", "label": "Module"}, {"qn": "pkg.unused_name", "label": "Function"}]
            hits = text_hits(repo, seeds)
        self.assertEqual(sorted(hits), ["Size", "unused_name", "warn_once"])  # modules are not searched
        self.assertEqual(hits["warn_once"]["hits"], [["a.py", 1], ["a.py", 3], ["c.md", 1]])  # one hit per line
        self.assertEqual(hits["warn_once"]["total"], 3)
        self.assertEqual(hits["warn_once"]["seeds"], ["pkg.log.warn_once"])
        self.assertEqual(hits["Size"]["total"], 1)
        self.assertEqual(hits["unused_name"]["total"], 0)


if __name__ == "__main__":
    unittest.main()
