import hashlib
import json
import logging
import os
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path

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
