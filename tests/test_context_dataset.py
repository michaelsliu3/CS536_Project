import gzip
import tempfile
import unittest
from pathlib import Path

from src.tracer_context.dataset import (
    append_record, build_key, entry_stats, group_by_repo, latest_by_id, level_rows, load_records,
    read_records, rewrite, run, trace_paths,
)


def entry(index, repo="o/r", number=None):
    number = number if number is not None else index + 1
    return {"index": index, "id": f"{repo.replace('/', '__')}__{number}",
            "record": {"githubPrUrl": f"https://github.com/{repo}/pull/{number}", "source_commit": "s",
                       "target_commit": f"t{index}", "project_main_language": "Go"}}


def record(e, status="ok", key="k1", text="ctx"):
    return {"id": e["id"], "index": e["index"], "status": status, "build": {"key": key},
            "levels": {"L01_min": {"text": text, "input_tokens": 10, "included": [], "included_by_kind": {}}}}


class FileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "out" / "context_levels.jsonl.gz"

    def tearDown(self):
        self._tmp.cleanup()

    def test_append_read_latest_and_rewrite_in_dataset_order(self):
        a, b = entry(1), entry(0)
        append_record(self.path, record(a, text="old"))
        append_record(self.path, record(b))
        append_record(self.path, record(a, text="new"))
        records, truncated = read_records(self.path)
        self.assertEqual((len(records), truncated), (3, False))
        self.assertEqual(load_records(self.path)[a["id"]]["levels"]["L01_min"]["text"], "new")
        rewrite(self.path, records)
        self.assertEqual([r["id"] for r in read_records(self.path)[0]], [b["id"], a["id"]])  # index order, deduped

    def test_cut_off_tail_is_detected_and_status_filter(self):
        append_record(self.path, record(entry(0)))
        append_record(self.path, record(entry(1), status="failed"))
        with open(self.path, "ab") as handle:  # a member cut off mid-write
            handle.write(gzip.compress(b'{"id": "x", "levels": {"L01')[:-6])
        records, truncated = read_records(self.path)
        self.assertTrue(truncated)
        self.assertEqual(len(records), 2)
        self.assertEqual(list(load_records(self.path)), ["o__r__1"])  # ok records only by default
        self.assertEqual(len(load_records(self.path, status=None)), 2)

    def test_level_rows(self):
        rows = level_rows([record(entry(0))])
        self.assertEqual(rows[0]["level"], "L01_min")
        self.assertEqual(rows[0]["text"], "ctx")
        self.assertNotIn("text", level_rows([record(entry(0))], include_text=False)[0])
        self.assertNotIn("included", rows[0])


class RunTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.out, self.repos = root / "context_levels.jsonl.gz", root / "repos"

    def tearDown(self):
        self._tmp.cleanup()

    def test_groups_by_repo_deletes_clones_and_resumes(self):
        entries = [entry(0, "a/x"), entry(1, "b/y"), entry(2, "a/x"), entry(3, "b/y")]
        self.assertEqual([[e["index"] for e in g] for _, g in group_by_repo(entries)], [[0, 2], [1, 3]])
        processed, seen_repos = [], []

        def process(e):
            name = e["id"].rsplit("__", 1)[0]
            (self.repos / name).mkdir(parents=True, exist_ok=True)  # what prepare_checkout would do
            seen_repos.append(sorted(p.name for p in self.repos.iterdir()))
            processed.append(e["index"])
            return record(e, status="failed" if e["index"] == 3 else "ok")

        counts = run(entries, process, lambda e: "k1", self.out, self.repos)
        self.assertEqual(processed, [0, 2, 1, 3])  # one repository at a time
        self.assertEqual(seen_repos[2], ["b__y"])   # a/x was deleted before b/y started
        self.assertEqual(list(self.repos.iterdir()), [])
        self.assertEqual(dict(counts), {"ok": 3, "failed": 1})

        processed.clear()
        counts = run(entries, process, lambda e: "k1", self.out, self.repos)
        self.assertEqual(processed, [3])  # only the failed entry is retried
        self.assertEqual(dict(counts), {"skipped": 3, "failed": 1})
        processed.clear()
        run(entries[:1], process, lambda e: "k2", self.out, self.repos)  # inputs changed: rebuilt
        self.assertEqual(processed, [0])
        self.assertEqual(len(read_records(self.out)[0]), 4)  # rewritten: one record per entry

    def test_keep_repos(self):
        def process(e):
            (self.repos / "o__r").mkdir(parents=True, exist_ok=True)
            return record(e)
        run([entry(0)], process, lambda e: "k", self.out, self.repos, keep_repos=True)
        self.assertTrue((self.repos / "o__r").is_dir())


class StatsTests(unittest.TestCase):
    TRACE = {
        "changed_files": ["a.py"], "hunks": {"a.py": [[1, 2], [5, 5]]}, "diff": "diff text",
        "seeds": [{"label": "Function"}, {"label": "Function"}, {"label": "Class"}],
        "callers": [{"hop": 1, "via_edge": "CALLS"}, {"hop": 1, "via_edge": "TESTS"}, {"hop": 2, "via_edge": "CALLS"}],
        "callees": [{"hop": 1}], "guide_files": ["README.md"],
        "text_hits": {"foo": {"total": 3, "hits": [["b.py", 4], ["c.py", 9]]}},
        "nodes": {"a.f": {"file": "a.py"}, "d.g": {"file": "d.py"}}, "index": {"nodes": 10, "edges": 20},
    }

    def test_input_side_stats_and_paths(self):
        stats = entry_stats(self.TRACE, {"title": "t", "body": "body"})
        self.assertEqual((stats["changed_files"], stats["hunks"], stats["changed_symbols"]), (1, 2, 3))
        self.assertEqual(stats["changed_symbols_by_label"], {"Class": 1, "Function": 2})
        self.assertEqual(stats["callers_by_hop"], {"1": 2, "2": 1})
        self.assertEqual(stats["test_callers_by_hop"], {"1": 1})
        self.assertEqual((stats["search_names"], stats["search_hits"], stats["pr_body_chars"]), (1, 3, 4))
        self.assertEqual(trace_paths(self.TRACE), ["README.md", "a.py", "b.py", "c.py", "d.py"])

    def test_build_key_tracks_inputs(self):
        cfg = {"tracer": {"workers": 4, "index_mode": "full", "callers": {}, "callees": {}}, "level": {}}
        base = build_key({"L01": cfg}, "tool 1", {"t": "tok@1"}, entry(0)["record"])
        self.assertEqual(base, build_key({"L01": cfg}, "tool 1", {"t": "tok@1"}, entry(0)["record"]))
        self.assertNotEqual(base, build_key({"L01": cfg}, "tool 2", {"t": "tok@1"}, entry(0)["record"]))
        self.assertNotEqual(base, build_key({"L01": cfg}, "tool 1", {"t": "tok@2"}, entry(0)["record"]))
        self.assertNotEqual(base, build_key({"L01": cfg}, "tool 1", {"t": "tok@1"}, entry(1)["record"]))
        self.assertEqual(latest_by_id([{"id": "a", "v": 1}, {"id": "a", "v": 2}])["a"]["v"], 2)


if __name__ == "__main__":
    unittest.main()
