import hashlib
import http.server
import importlib.util
import json
import os
import struct
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "setup" / "download_reviewer_model.py"
spec = importlib.util.spec_from_file_location("download_reviewer_model", SCRIPT)
dl = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = dl
spec.loader.exec_module(dl)


def write_shard(path: Path, tensors: dict[str, int]) -> None:
    """A minimal safetensors file: 8-byte header length, JSON header, then the data."""
    header, offset = {}, 0
    for name, nbytes in tensors.items():
        header[name] = {"dtype": "U8", "shape": [nbytes], "data_offsets": [offset, offset + nbytes]}
        offset += nbytes
    raw = json.dumps(header).encode()
    path.write_bytes(struct.pack("<Q", len(raw)) + raw + b"\0" * offset)


class DownloadReviewerModelTest(unittest.TestCase):
    def test_reads_the_cloud_reviewer_from_the_notebook(self):
        reviewer = dl.read_notebook_profile("cloud")["reviewer"]
        self.assertEqual(reviewer["backend"], "openai")
        self.assertEqual(reviewer["model"], "Qwen/Qwen3.8-27B")
        self.assertRegex(reviewer["model_revision"], dl.COMMIT_HASH)
        with self.assertRaises(ValueError):
            dl.read_notebook_profile("no-such-profile")

    def test_serve_command_matches_the_notebook(self):
        reviewer = dl.read_notebook_profile("cloud")["reviewer"]
        command = dl.serve_command(reviewer["model"], reviewer["model_revision"], 262144, reviewer, "/hf")
        self.assertTrue(command.startswith("HF_HOME=/hf "))
        self.assertIn("HF_HUB_OFFLINE=1", command)
        self.assertIn(f"vllm serve {reviewer['model']} --revision {reviewer['model_revision']} "
                      f"--tokenizer-revision {reviewer['model_revision']}", command)
        self.assertIn("--port 8000", command)  # the notebook's base_url

    def test_serve_command_keeps_vllm_on_the_mirror(self):
        reviewer = dl.read_notebook_profile("cloud")["reviewer"]
        command = dl.serve_command(reviewer["model"], reviewer["model_revision"], 262144, reviewer, "/hf")
        self.assertIn("HF_ENDPOINT=https://hf-mirror.com", command)

    def test_official_hosts(self):
        for host in ("huggingface.co", "cdn-lfs.huggingface.co", "hf.co", "HuggingFace.co."):
            self.assertTrue(dl.is_official_host(host), host)
            self.assertFalse(dl.is_file_cdn_host(host), host)
        for host in ("hf-mirror.com", "cdn.hf-mirror.com", "nothf.co", "huggingface.co.example.cn", "127.0.0.1", None):
            self.assertFalse(dl.is_official_host(host), host)
            self.assertFalse(dl.is_file_cdn_host(host), host)

    def test_file_cdn_hosts_are_not_the_hub(self):
        # hf-mirror.com redirects large files to these; they answer from mainland China.
        for host in ("cas-bridge.xethub.hf.co", "cdn-lfs.hf.co", "us.aws.cdn.hf.co", "CDN-LFS-US-1.hf.co."):
            self.assertTrue(dl.is_file_cdn_host(host), host)
            self.assertFalse(dl.is_official_host(host), host)

    def test_guard_refuses_the_official_hub_through_a_mirror(self):
        guard = dl.NetworkGuard(block_official=True)
        guard.check("hf-mirror.com", "https://hf-mirror.com/api/models/x")
        guard.check("hf-mirror.com", "https://hf-mirror.com/x/resolve/r/config.json")
        with self.assertRaises(dl.OfficialHubBlocked):
            guard.check("cdn-lfs.huggingface.co", "https://cdn-lfs.huggingface.co/x?token=secret")
        with self.assertRaises(dl.OfficialHubBlocked):
            guard.check("huggingface.co", via="dns")
        with self.assertRaises(dl.OfficialHubBlocked):
            guard.check("hf.co", via="dns")
        self.assertEqual(guard.blocked, "cdn-lfs.huggingface.co (https://cdn-lfs.huggingface.co/x)")  # no token
        self.assertEqual(guard.requests, {"hf-mirror.com": 2})
        self.assertIn("official Hugging Face hub (huggingface.co) never contacted", guard.summary())

    def test_guard_allows_the_file_cdn_through_a_mirror(self):
        guard = dl.NetworkGuard(block_official=True)
        guard.check("hf-mirror.com", "https://hf-mirror.com/x/resolve/r/model.safetensors")
        guard.check("cas-bridge.xethub.hf.co", via="dns")
        guard.check("cas-bridge.xethub.hf.co", "https://cas-bridge.xethub.hf.co/x?Signature=secret")
        self.assertIsNone(guard.blocked)
        self.assertEqual(guard.requests, {"hf-mirror.com": 1, "cas-bridge.xethub.hf.co": 1})
        summary = guard.summary()
        self.assertIn("never contacted", summary)
        self.assertIn("file CDN (cas-bridge.xethub.hf.co)", summary)
        self.assertNotIn("secret", summary)

    def test_guard_only_logs_with_the_official_endpoint(self):
        guard = dl.NetworkGuard(block_official=False)
        guard.check("huggingface.co", "https://huggingface.co/api/models/x")
        self.assertIsNone(guard.blocked)
        self.assertIn("CONTACTED", guard.summary())

    def test_git_blob_sha1_matches_git(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "f"
            path.write_bytes(b"hello\n")
            self.assertEqual(dl.git_blob_sha1(path), "ce013625030ba8dba906f756967f9e9ca394464a")  # git hash-object

    def test_check_safetensors_finds_truncated_shards_and_missing_tensors(self):
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp)
            write_shard(snapshot / "s1.safetensors", {"a": 4, "b": 8})
            index = {"metadata": {"total_size": 12}, "weight_map": {"a": "s1.safetensors", "b": "s1.safetensors"}}
            (snapshot / "model.safetensors.index.json").write_text(json.dumps(index))
            self.assertEqual(dl.check_safetensors(snapshot), [])

            index["weight_map"]["c"] = "s1.safetensors"
            index["weight_map"]["d"] = "s2.safetensors"
            (snapshot / "model.safetensors.index.json").write_text(json.dumps(index))
            problems = dl.check_safetensors(snapshot)
            self.assertTrue(any("lacks 1 tensors" in p for p in problems))
            self.assertTrue(any("s2.safetensors is missing" in p for p in problems))

            shard = snapshot / "s1.safetensors"
            shard.write_bytes(shard.read_bytes()[:-3])
            self.assertTrue(any("header needs" in p for p in dl.check_safetensors(snapshot)))

    def test_remove_temp_files_keeps_resumable_and_live_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            blobs = Path(tmp)
            orphan, live, resumable = (blobs / "ab12.0123abcd.incomplete", blobs / "cd34.89abcdef.incomplete",
                                       blobs / "ef56.incomplete")
            for path in (orphan, live, resumable):
                path.write_bytes(b"x" * 10)
            old = time.time() - 3600
            os.utime(orphan, (old, old))
            os.utime(resumable, (old, old))
            self.assertEqual(dl.remove_temp_files(blobs, older_than=60), 10)
            self.assertFalse(orphan.exists())
            self.assertTrue(live.exists() and resumable.exists())
            self.assertEqual(dl.partial_bytes(blobs, resumable_only=True), 10)


class Origin(http.server.BaseHTTPRequestHandler):
    """A file server with Range support whose faults the tests switch on."""
    protocol_version = "HTTP/1.1"
    payload = b""
    ranges: list = []  # the Range header of every request, in order
    slow_first = 0  # this many first requests are answered at about 20 KB/s
    close_early_first = 0  # this many first requests get half the bytes, then the connection closes
    ignore_range = False  # always answer 200 with the whole file
    error_status = None  # answer every request with this status
    redirect = None  # answer every request with a 302 to this URL

    def do_GET(self):
        cls = type(self)
        number = len(cls.ranges)
        cls.ranges.append(self.headers.get("Range"))
        if cls.error_status or cls.redirect:
            self.send_response(cls.error_status or 302)
            if cls.redirect:
                self.send_header("Location", cls.redirect)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        requested = self.headers.get("Range")
        if requested and not cls.ignore_range:
            start = int(requested.removeprefix("bytes=").rstrip("-"))
            body = cls.payload[start:]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(cls.payload) - 1}/{len(cls.payload)}")
        else:
            body = cls.payload
            self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            if number < cls.close_early_first:
                self.wfile.write(body[: len(body) // 2])
                self.close_connection = True
            elif number < cls.slow_first:
                for start in range(0, len(body), 1024):
                    self.wfile.write(body[start:start + 1024])
                    self.wfile.flush()
                    time.sleep(0.05)
            else:
                self.wfile.write(body)
        except OSError:  # the client replaced this connection
            self.close_connection = True

    def log_message(self, *args):
        pass


class FetchWeightFileTest(unittest.TestCase):
    def setUp(self):
        Origin.payload = os.urandom(300_000)
        Origin.ranges = []
        Origin.slow_first = Origin.close_early_first = 0
        Origin.ignore_range, Origin.error_status, Origin.redirect = False, None, None
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Origin)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.blobs = Path(tmp.name) / "blobs"
        self.file = dl.RemoteFile("w.safetensors", len(Origin.payload), hashlib.sha256(Origin.payload).hexdigest(), None)
        self.blob, self.part = self.blobs / self.file.sha256, self.blobs / f"{self.file.sha256}.incomplete"
        self.ctx = {"endpoint": f"http://127.0.0.1:{self.server.server_address[1]}", "repo": "org/model",
                    "revision": "r", "blobs": self.blobs, "snapshot": Path(tmp.name) / "snapshots" / "r",
                    "stop": threading.Event(), "min_speed": 0, "direct": True, "cache": tmp.name, "token": False}
        for name, value in (("BACKOFF_SECONDS", 0.01), ("WINDOW_SECONDS", 0.2), ("SLOW_STREAK", 3)):
            patcher = mock.patch.object(dl, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fetch(self, retries=3):
        return dl.fetch_weight_file(self.file, self.ctx, retries)

    def test_downloads_into_the_blobs_folder(self):
        self.assertEqual(self.fetch(), self.blob)
        self.assertEqual(self.blob.read_bytes(), Origin.payload)
        self.assertFalse(self.part.exists())
        self.assertEqual(Origin.ranges, ["bytes=0-"])

    def test_resumes_a_partial_file_from_an_earlier_run(self):
        self.blobs.mkdir()
        self.part.write_bytes(Origin.payload[:100_000])
        self.fetch()
        self.assertEqual(Origin.ranges, ["bytes=100000-"])
        self.assertEqual(self.blob.read_bytes(), Origin.payload)

    def test_a_finished_blob_is_not_downloaded_again(self):
        self.blobs.mkdir()
        self.blob.write_bytes(Origin.payload)
        self.assertEqual(self.fetch(), self.blob)
        self.assertEqual(Origin.ranges, [])

    def test_replaces_a_slow_connection_and_resumes(self):
        Origin.slow_first = 1
        self.ctx["min_speed"] = 1e6
        with self.assertLogs(dl.log, "INFO") as logs:
            self.fetch()
        self.assertEqual(self.blob.read_bytes(), Origin.payload)
        self.assertEqual(len(Origin.ranges), 2)
        resumed_at = int(Origin.ranges[1].removeprefix("bytes=").rstrip("-"))
        self.assertTrue(0 < resumed_at < len(Origin.payload), resumed_at)  # kept what the slow connection brought
        self.assertTrue(any("replacing it" in line for line in logs.output))

    def test_slow_connections_are_kept_when_the_check_is_off(self):
        Origin.payload = Origin.payload[:20_000]  # about one second at the server's slow pace
        self.file = dl.RemoteFile("w.safetensors", 20_000, hashlib.sha256(Origin.payload).hexdigest(), None)
        Origin.slow_first = 99
        self.fetch()
        self.assertEqual(Origin.ranges, ["bytes=0-"])

    def test_stops_replacing_when_every_connection_is_slow(self):
        Origin.payload = Origin.payload[:30_000]
        self.file = dl.RemoteFile("w.safetensors", 30_000, hashlib.sha256(Origin.payload).hexdigest(), None)
        Origin.slow_first = 99
        self.ctx["min_speed"] = 1e6
        with mock.patch.object(dl, "SLOW_STREAK", 2), self.assertLogs(dl.log, "WARNING") as logs:
            blob = self.fetch()
        self.assertEqual(blob.read_bytes(), Origin.payload)
        self.assertEqual(len(Origin.ranges), 3)  # two replaced, the third kept to the end
        self.assertTrue(any("in a row" in line for line in logs.output))

    def test_starts_again_when_the_server_ignores_the_range(self):
        Origin.ignore_range = True
        self.blobs.mkdir()
        self.part.write_bytes(b"\0" * 100_000)
        with self.assertLogs(dl.log, "WARNING"):
            self.fetch()
        self.assertEqual(self.blob.read_bytes(), Origin.payload)

    def test_resumes_after_the_connection_closes_early(self):
        Origin.close_early_first = 1
        with self.assertLogs(dl.log, "WARNING"):
            self.fetch()
        self.assertEqual(Origin.ranges, ["bytes=0-", "bytes=150000-"])
        self.assertEqual(self.blob.read_bytes(), Origin.payload)

    def test_gives_up_after_repeated_failures_and_keeps_the_partial_file(self):
        self.blobs.mkdir()
        self.part.write_bytes(Origin.payload[:1000])
        Origin.error_status = 503
        with self.assertLogs(dl.log, "WARNING"), self.assertRaises(OSError):
            self.fetch(retries=3)
        self.assertEqual(len(Origin.ranges), 3)
        self.assertEqual(self.part.read_bytes(), Origin.payload[:1000])
        self.assertFalse(self.blob.exists())

    def test_refuses_a_redirect_to_the_official_hub(self):
        Origin.redirect = "https://huggingface.co/org/model/resolve/r/w.safetensors"
        with mock.patch.object(dl, "_guard", dl.NetworkGuard(block_official=True)), self.assertLogs(dl.log, "ERROR"):
            with self.assertRaises(dl.OfficialHubBlocked):
                self.fetch()
            self.assertEqual(dl._guard.requests, {"127.0.0.1": 1})  # huggingface.co was never contacted
        self.assertFalse(self.blob.exists())

    def test_two_runs_cannot_write_the_same_partial_file(self):
        try:
            import fcntl
        except ImportError:
            self.skipTest("no flock on this platform")
        self.blobs.mkdir()
        with open(self.part, "wb") as other:
            fcntl.flock(other.fileno(), fcntl.LOCK_EX)
            with self.assertRaisesRegex(RuntimeError, "another download"):
                self.fetch()
        self.assertEqual(Origin.ranges, [])

    def test_download_all_fetches_the_blob_and_lets_the_hub_link_it(self):
        try:
            import huggingface_hub
        except ImportError:
            self.skipTest("huggingface_hub is not installed")
        calls = []

        def link_only(repo, name, revision, cache_dir, token, force_download):
            calls.append(force_download)
            pointer = self.ctx["snapshot"] / name
            pointer.parent.mkdir(parents=True, exist_ok=True)
            pointer.symlink_to(self.blob)  # what huggingface_hub does when the blob is already there
            return str(pointer)

        self.file.force = True  # as after a failed verification: any old copy must go first
        self.blobs.mkdir()
        self.part.write_bytes(b"stale")
        progress = dl.Progress([self.file], 0, self.blobs)
        with mock.patch.object(huggingface_hub, "hf_hub_download", link_only):
            failed = dl.download_all([self.file], self.ctx, progress, SimpleNamespace(retries=3, workers=2))
        self.assertEqual(failed, [])
        self.assertEqual(calls, [False])  # the hub must not download the file a second time
        self.assertEqual((self.ctx["snapshot"] / "w.safetensors").read_bytes(), Origin.payload)
        self.assertEqual(Origin.ranges, ["bytes=0-"])  # the stale partial file was not resumed


if __name__ == "__main__":
    unittest.main()
