import importlib.util
import json
import os
import struct
import sys
import tempfile
import time
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
