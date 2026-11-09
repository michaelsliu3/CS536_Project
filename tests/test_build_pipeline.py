import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from src.tracer_context import dataset, pipeline

LOG = logging.getLogger("test_build_pipeline")
LOG.addHandler(logging.NullHandler())
GIT_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def entry(index, repo="o/r"):
    return {"index": index, "id": f"{repo.replace('/', '__')}__{index + 1}",
            "record": {"githubPrUrl": f"https://github.com/{repo}/pull/{index + 1}", "source_commit": "s",
                       "target_commit": f"t{index}", "project_main_language": "Go"}}


def record(e, status="ok", stage=None):
    out = {"id": e["id"], "index": e["index"], "status": status, "build": {"key": "k"}, "language": "Go",
           "levels": {"L01_min": {"text": "t", "input_tokens": 5}}}
    if status != "ok":
        out.update(failed_stage=stage, error="boom")
    return out


class Temp(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()


class LockTests(Temp):
    def test_live_lock_blocks_and_stale_lock_is_taken_over(self):
        with pipeline.BuildLock(self.root) as lock:
            info = json.loads(lock.path.read_text())
            self.assertEqual(info["pid"], os.getpid())
            with self.assertRaisesRegex(pipeline.SetupError, "another build"):
                pipeline.BuildLock(self.root).__enter__()
        self.assertFalse((self.root / ".build.lock").exists())
        dead = subprocess.Popen(["true"])
        dead.wait()
        (self.root / ".build.lock").write_text(json.dumps({"pid": dead.pid, "host": socket.gethostname()}))
        with pipeline.BuildLock(self.root, LOG):
            self.assertEqual(json.loads((self.root / ".build.lock").read_text())["pid"], os.getpid())
        (self.root / ".build.lock").write_text("not json")  # unreadable lock: treated as stale
        with pipeline.BuildLock(self.root, LOG):
            pass


class IgnoreAndDownloadTests(Temp):
    def test_root_inside_repo_is_made_ignored(self):
        subprocess.run(["git", "-C", str(self.root), "init", "-q"], check=True, env=GIT_ENV)
        ignored = self.root / "data"
        ignored.mkdir()
        (self.root / ".gitignore").write_text("data/*\n")
        pipeline.ensure_ignored(ignored, self.root, LOG)
        self.assertFalse((ignored / ".gitignore").exists())  # already ignored: nothing written
        plain = self.root / "datasets" / "aacr"
        plain.mkdir(parents=True)
        pipeline.ensure_ignored(plain, self.root, LOG)
        self.assertEqual((plain / ".gitignore").read_text().splitlines()[-1], "*")
        outside = Path(tempfile.mkdtemp())
        pipeline.ensure_ignored(outside, self.root, LOG)  # outside the project: left alone
        self.assertFalse((outside / ".gitignore").exists())
        outside.rmdir()

    def test_download_verifies_checksum(self):
        source = self.root / "src.json"
        source.write_text("[1, 2]")
        good = hashlib.sha256(b"[1, 2]").hexdigest()
        dest = pipeline.download(source.as_uri(), self.root / "out" / "a.json", good, LOG)
        self.assertEqual(dest.read_text(), "[1, 2]")
        with self.assertRaisesRegex(pipeline.SetupError, "checksum mismatch"):
            pipeline.download(source.as_uri(), self.root / "b.json", "0" * 64, LOG)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["out", "src.json"])  # no partial file left
        with self.assertRaisesRegex(pipeline.SetupError, "download failed"):
            pipeline.download((self.root / "missing").as_uri(), self.root / "c.json", good, LOG)

    def test_dataset_file_offline_and_mismatch(self):
        with self.assertRaisesRegex(pipeline.SetupError, "download disabled"):
            pipeline.ensure_dataset_file(self.root, LOG, allow_download=False)
        (self.root / pipeline.AACR_FILE).write_text("[]")
        with self.assertLogs(LOG, level="WARNING"):
            self.assertEqual(pipeline.ensure_dataset_file(self.root, LOG, allow_download=False).name, pipeline.AACR_FILE)

    def test_disk_and_durations(self):
        self.assertGreater(pipeline.require_free_disk(self.root, 0), 0)
        with self.assertRaisesRegex(pipeline.SetupError, "GB free"):
            pipeline.require_free_disk(self.root, 1e12)
        self.assertEqual([pipeline.format_duration(s) for s in (5, 75, 3725)], ["5s", "1m15s", "1h02m"])


class RunTests(Temp):
    def test_retries_delete_half_fetched_clone_then_succeed(self):
        out, repos = self.root / "c.jsonl.gz", self.root / "repos"
        calls, waits = [], []

        def process(e):
            calls.append(e["index"])
            (repos / "o__r").mkdir(parents=True, exist_ok=True)
            return record(e, "failed", "checkout") if len(calls) == 1 else record(e)

        retried = []
        counts = dataset.run([entry(0)], process, lambda e: "k", out, repos, retries=2, retry_wait=5,
                             on_retry=lambda r, attempt: retried.append((r["failed_stage"], attempt)),
                             sleep=waits.append)
        self.assertEqual((calls, waits, retried), ([0, 0], [5], [("checkout", 1)]))
        self.assertEqual(dict(counts), {"ok": 1})

    def test_retries_exhausted_record_failure_with_backoff(self):
        waits = []
        counts = dataset.run([entry(0)], lambda e: record(e, "failed", "trace"), lambda e: "k",
                             self.root / "c.jsonl.gz", self.root / "repos", retries=2, retry_wait=3,
                             sleep=waits.append)
        self.assertEqual((dict(counts), waits), ({"failed": 1}, [3, 6]))
        self.assertEqual(dataset.load_records(self.root / "c.jsonl.gz", status=None)["o__r__1"]["status"], "failed")

    def test_interrupted_rewrite_keeps_the_file_and_leaves_no_partial_copy(self):
        out = self.root / "c.jsonl.gz"
        dataset.append_record(out, record(entry(0)))
        before = out.read_bytes()
        with mock.patch.object(dataset.json, "dumps", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            dataset.rewrite(out, [record(entry(0))])
        self.assertEqual((out.read_bytes(), sorted(p.name for p in self.root.iterdir())), (before, ["c.jsonl.gz"]))

    def test_commits_gone_from_github_are_not_retried(self):
        calls, waits = [], []

        def process(e):
            calls.append(e["index"])
            return {**record(e, "failed", "merge_base"), "retryable": False}

        counts = dataset.run([entry(0)], process, lambda e: "k", self.root / "c.jsonl.gz", self.root / "repos",
                             retries=2, retry_wait=5, sleep=waits.append)
        self.assertEqual((calls, waits, dict(counts)), ([0], [], {"failed": 1}))

    def test_before_repo_can_stop_cleanly_and_progress_is_kept(self):
        out = self.root / "c.jsonl.gz"
        entries = [entry(0, "a/x"), entry(1, "b/y")]

        def before(name, group):
            if name == "b__y":
                raise dataset.StopBuild("low disk")

        with self.assertRaises(dataset.StopBuild):
            dataset.run(entries, record, lambda e: "k", out, self.root / "repos", before_repo=before)
        self.assertEqual(list(dataset.load_records(out)), ["a__x__1"])  # finished work is saved
        processed = []
        dataset.run(entries, lambda e: processed.append(e["id"]) or record(e), lambda e: "k", out,
                    self.root / "repos")
        self.assertEqual(processed, ["b__y__2"])  # the resumed run only does what is left


class BuildKeyTests(unittest.TestCase):
    def test_fetch_version_is_part_of_the_resume_key(self):
        configs = {"L01_min": {"tracer": {}, "level": {"parent": None}}}
        rec = entry(0)["record"]
        with mock.patch.object(dataset, "tracer_settings", lambda cfg: {}), \
                mock.patch.object(dataset, "config_sha256", lambda cfg: "c"):
            now = dataset.build_key(configs, "0.12.1", {"t": "tok"}, rec)
            with mock.patch.object(dataset, "FETCH_VERSION", 1):
                before = dataset.build_key(configs, "0.12.1", {"t": "tok"}, rec)
        self.assertNotEqual(now, before)  # PRs built with the old merge base are rebuilt on resume


class CleanTests(Temp):
    SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "dataset" / "clean_dataset.py"

    def make_build(self):
        r = self.root
        (r / pipeline.OUTPUT_FILE).write_bytes(b"x" * 10)
        (r / f"{pipeline.OUTPUT_FILE}abc123").write_bytes(b"y")  # left by an interrupted rewrite
        (r / pipeline.AACR_FILE).write_text("[]")
        for sub in ("repos/o__r/.git", "tmp/cbm-cache-1", "tools/bin"):
            (r / ".work" / sub).mkdir(parents=True)
        (r / ".work/repos/o__r/.git/HEAD").write_text("ref")
        (r / "logs").mkdir()
        (r / "logs/build_1.log").write_text("log")
        (r / "logs/notes.txt").write_text("not the builder's")
        (r / ".gitignore").write_text(pipeline._GITIGNORE_TEXT)
        (r / "keep.txt").write_text("not the builder's")

    def names(self, paths):
        return sorted(str(p.relative_to(self.root)) for p in paths)

    def remaining(self):
        return sorted(str(p.relative_to(self.root)) for p in self.root.rglob("*"))

    def clean(self, *args, root=None):
        # Always a temporary runner folder: the default is the project's real data/aacr_tracer.
        return subprocess.run([sys.executable, str(self.SCRIPT), str(root or self.root),
                               "--runner-work", str(self.runner), *args],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL)

    @property
    def runner(self):
        return self.root.parent / f"{self.root.name}-runner"

    def make_runner(self):
        for clone in ("repos/o__r/.git", "repos/p__q/.git", "repos/not_a_clone", "entries/o__r__1"):
            (self.runner / clone).mkdir(parents=True)
        (self.runner / "entries/o__r__1/status.json").write_text("{}")
        self.addCleanup(shutil.rmtree, self.runner, True)

    def test_runner_clones_lists_only_git_folders(self):
        self.make_runner()
        self.assertEqual([p.name for p in pipeline.runner_clones(self.runner)], ["o__r", "p__q"])
        self.assertEqual(pipeline.runner_clones(self.root / "missing"), [])

    def test_script_deletes_runner_clones_and_keeps_its_results(self):
        self.make_runner()
        empty_root = self.root / "never-built"  # no dataset build: only the runner's clones go
        done = self.clean("--yes", root=empty_root)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("inspection runner clones", done.stdout)
        self.assertEqual(sorted(str(p.relative_to(self.runner)) for p in self.runner.rglob("*")),
                         ["entries", "entries/o__r__1", "entries/o__r__1/status.json", "repos", "repos/not_a_clone"])
        nothing = self.clean("--yes", root=empty_root)
        self.assertEqual((nothing.returncode, "nothing to delete" in nothing.stderr), (0, True))

    def test_targets_by_scope(self):
        self.make_build()
        built = [".work/repos", ".work/tmp", "context_levels.jsonl.gz", "context_levels.jsonl.gzabc123"]
        self.assertEqual(self.names(pipeline.clean_targets(self.root)), built)
        self.assertEqual(self.names(pipeline.clean_targets(self.root, logs=True)), built + ["logs/build_1.log"])
        self.assertEqual(self.names(pipeline.clean_targets(self.root, everything=True)),
                         [".gitignore", ".work", "context_levels.jsonl.gz", "context_levels.jsonl.gzabc123",
                          "logs/build_1.log", "positive_samples.json"])

    def test_remove_keeps_files_that_are_not_the_builders(self):
        self.make_build()
        pipeline.remove_paths(self.root, pipeline.clean_targets(self.root, everything=True))
        self.assertEqual(self.remaining(), ["keep.txt", "logs", "logs/notes.txt"])

    def test_a_folder_without_build_files_is_refused(self):
        (self.root / pipeline.AACR_FILE).write_text("[]")
        with self.assertRaisesRegex(pipeline.SetupError, "no dataset build files"):
            pipeline.clean_targets(self.root)
        self.assertEqual(self.names(pipeline.clean_targets(self.root, everything=True)), ["positive_samples.json"])

    def test_script_lists_asks_and_deletes(self):
        self.make_build()
        before = self.remaining()
        dry = self.clean("--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertIn("context_levels.jsonl.gz", dry.stdout)
        no_terminal = self.clean()  # cannot ask without a terminal: deletes nothing
        self.assertEqual((no_terminal.returncode, self.remaining()), (1, before))
        done = self.clean("--yes")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.remaining(), [".gitignore", ".work", ".work/tools", ".work/tools/bin", "keep.txt",
                                            "logs", "logs/build_1.log", "logs/notes.txt", "positive_samples.json"])

    def test_script_refuses_while_a_build_runs(self):
        self.make_build()
        before = self.remaining()
        with pipeline.BuildLock(self.root):
            result = self.clean("--yes", "--all")
        self.assertEqual(result.returncode, 1)
        self.assertIn("another build", result.stderr)
        self.assertEqual(self.remaining(), before)


class ProgressTests(unittest.TestCase):
    def test_lines_counts_and_eta(self):
        with self.assertLogs("progress-test", level="DEBUG") as captured:
            logger = logging.getLogger("progress-test")
            progress = pipeline.Progress(logger, total=3, to_build=2)
            progress(record(entry(0)), "skipped", 0)
            progress(record(entry(1)), "ok", 10)
            progress({**record(entry(2), "failed", "trace"), "traceback": "Traceback..."}, "failed", 30)
        text = "\n".join(captured.output)
        self.assertIn("[2/3] o__r__2 (Go) ok in 10s: 1 levels, 5-5 input tokens | 1 built, 0 failed, 1 skipped", text)
        self.assertIn("about 10s left", text)
        self.assertIn("FAILED at trace", text)
        self.assertIn("Traceback...", text)
        self.assertEqual(progress.counts, {"ok": 1, "failed": 1, "skipped": 1})


if __name__ == "__main__":
    unittest.main()
