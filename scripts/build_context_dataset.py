"""Build the one-file context-level dataset for AACR-Bench entries (no LLM; runs on a laptop).

For each selected entry, grouped by repository: fetch only the PR's base and reviewed commits,
run the per-entry checks, fetch the PR title and description (authenticated `gh`), trace once
with codebase-memory-mcp, and pack every level config. One JSON line per PR is appended to
--out (default data/aacr_tracer/context_levels.jsonl.gz). Each repository clone is deleted
after its last selected entry unless --keep-repos.

Re-running resumes: PRs already built with the same inputs, tool, configs and tokenizer are
skipped; failed ones are retried. See src/tracer_context/dataset.py for the record format.

  .venv/bin/python scripts/build_context_dataset.py --list --languages Go
  .venv/bin/python scripts/build_context_dataset.py --entries cline__cline__5177,ollama__ollama__12185
  .venv/bin/python scripts/build_context_dataset.py --all
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.tracer_context import dataset  # noqa: E402
from src.tracer_context.aacr import file_sha256, load_entries, select_entries  # noqa: E402
from src.tracer_context.runtime import load_level_configs, make_counter  # noqa: E402

DEFAULT_DATASET = PROJECT_ROOT / "data" / "codebase_memory_trial" / "aacr" / "positive_samples.json"
DEFAULT_WORK = PROJECT_ROOT / "data" / "aacr_tracer"


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET, help="AACR-Bench positive_samples.json")
    parser.add_argument("--work", type=Path, default=DEFAULT_WORK, help="Folder for temporary clones and caches")
    parser.add_argument("--out", type=Path, help="Output file (default: <work>/context_levels.jsonl.gz)")
    parser.add_argument("--config", type=Path, action="append",
                        help="Level config, repeatable, parents first (default: configs/levels/*.yaml, sorted)")
    parser.add_argument("--entries", help="Comma-separated entry IDs (owner__repo__number) or 0-based file positions")
    parser.add_argument("--languages", help="Comma-separated main languages, e.g. Go,TypeScript")
    parser.add_argument("--limit", type=int, help="At most this many selected entries")
    parser.add_argument("--all", action="store_true", help="Run every entry (required when nothing is selected)")
    parser.add_argument("--list", action="store_true", help="Print the selected entries and their status, then exit")
    parser.add_argument("--force", action="store_true", help="Rebuild entries even when their record is current")
    parser.add_argument("--keep-repos", action="store_true", help="Keep repository clones after processing")
    parser.add_argument("--fetch-depth", type=int, default=50, help="Initial history depth to fetch")
    parser.add_argument("--max-depth", type=int, default=3200, help="Give up finding a merge base beyond this")
    args = parser.parse_args()
    work = args.work.resolve()
    out_path = (args.out or work / "context_levels.jsonl.gz").resolve()

    entries = load_entries(args.dataset)
    selected = select_entries(entries, ids=args.entries.split(",") if args.entries else None,
                              languages=args.languages.split(",") if args.languages else None, limit=args.limit)
    if args.list:
        records = dataset.load_records(out_path, status=None)
        for entry in selected:
            status = records.get(entry["id"], {}).get("status", "-")
            print(f"{entry['index']:>4}  {entry['id']:<55} {entry['record']['project_main_language']:<11} "
                  f"{entry['record']['change_line_count']:>5} lines  {status}")
        print(f"{len(selected)} entries in {len(dataset.group_by_repo(selected))} repositories")
        return
    if not (args.entries or args.languages or args.limit is not None or args.all):
        parser.error("select entries with --entries, --languages or --limit, or pass --all to run every entry")

    config_paths = args.config or sorted((PROJECT_ROOT / "configs" / "levels").glob("*.yaml"))
    configs = load_level_configs(config_paths)
    counters = {}
    for cfg in configs.values():  # load tokenizers before any fetching or indexing
        name = cfg["packing"]["tokenizer"]
        counters.setdefault(name, make_counter(name))
    tokenizers = {name: label for name, (_, label) in counters.items()}
    first = next(iter(configs.values()))
    binary = Path(first["tracer"]["binary"])
    binary = binary if binary.is_absolute() else PROJECT_ROOT / binary
    tool_version = subprocess.run([str(binary), "--version"], capture_output=True, text=True,
                                  check=True).stdout.strip()
    code_commit = subprocess.run(["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"], capture_output=True,
                                 text=True).stdout.strip()
    extra_build = {"dataset_sha256": file_sha256(args.dataset), "code_commit": code_commit}
    repos_dir, scratch_dir = work / "repos", work / "tmp"
    scratch_dir.mkdir(parents=True, exist_ok=True)

    def key_of(entry):
        return dataset.build_key(configs, tool_version, tokenizers, entry["record"])

    def process(entry):
        return dataset.process_entry(entry, configs, counters, binary=binary, tool_version=tool_version,
                                     repos_dir=repos_dir, scratch_dir=scratch_dir, key=key_of(entry),
                                     fetch_depth=args.fetch_depth, max_depth=args.max_depth, extra_build=extra_build)

    position = [0]

    def report(record, outcome, seconds):
        position[0] += 1
        if outcome == "ok":
            levels = record["levels"]
            detail = f"{len(levels)} levels, {min(l['input_tokens'] for l in levels.values())}-" \
                     f"{max(l['input_tokens'] for l in levels.values())} input tokens"
        elif outcome == "failed":
            detail = f"{record.get('failed_stage')}: {record.get('error', '')[:200]}"
        else:
            detail = "already built"
        print(f"[{position[0]}/{len(selected)}] {record['id']} ({record.get('language')}) {outcome} "
              f"in {seconds:.0f}s - {detail}", flush=True)

    print(f"{len(selected)} entries in {len(dataset.group_by_repo(selected))} repositories, levels {list(configs)}, "
          f"{tool_version}; writing {out_path}", flush=True)
    try:
        counts = dataset.run(selected, process, key_of, out_path, repos_dir, keep_repos=args.keep_repos,
                             force=args.force, report=report)
    finally:
        shutil.rmtree(scratch_dir, ignore_errors=True)
    total = dataset.load_records(out_path, status=None)
    size_mb = out_path.stat().st_size / 1e6 if out_path.exists() else 0
    print(f"done: {dict(counts)}; file has {sum(r['status'] == 'ok' for r in total.values())} ok and "
          f"{sum(r['status'] != 'ok' for r in total.values())} failed PRs, {size_mb:.1f} MB")


if __name__ == "__main__":
    main()
