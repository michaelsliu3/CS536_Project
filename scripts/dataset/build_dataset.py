"""Build the context-level dataset for AACR-Bench in one command (no LLM; runs on a laptop).

    .venv/bin/python scripts/dataset/build_dataset.py ROOT [options]

ROOT is the dataset folder. Everything lives there:
  ROOT/positive_samples.json       AACR-Bench entries (downloaded at a pinned commit if missing)
  ROOT/context_levels.jsonl        the output: one JSON line per PR with all context levels
  ROOT/logs/build_<time>.log       full log of each run (terminal output plus details)
  ROOT/.work/                      temporary clones and caches, removed as the build goes
  ROOT/.build.lock                 present while a build runs
If ROOT is inside this repository and not already ignored by git, a .gitignore is written there.

What a run does:
  1. Setup checks, each failing with a clear fix: git and gh (logged in), Python packages,
     the dataset file, the codebase-memory-mcp tool (pinned build downloaded and verified if
     missing), the reviewer tokenizer (tokenizer files downloaded if missing), no other tool
     session running, free disk space, and the level configs.
  2. For each PR, grouped by repository: get the PR diff's merge base from GitHub, fetch the
     reviewed commit and enough history to reach it, run the checks, fetch the PR
     title/description, trace once, pack all levels, append one line, and delete the
     repository's clone after its last PR. A failed PR is retried (--retries) and then
     recorded with its stage and error; the build continues.
  3. A summary of built, skipped and failed PRs.

Re-running the same command continues where it stopped: PRs already built with the same
inputs, fetch method, tool, configs and tokenizer are skipped, and failed ones are retried.
Stop with Ctrl+C (or `kill -INT <pid>`); the file is left consistent. To start over instead,
run scripts/dataset/clean_dataset.py ROOT.

Exit codes: 0 all PRs built; 2 finished with some failed PRs; 1 setup failed or the build
crashed; 3 stopped for low disk space; 130 interrupted.
"""
from __future__ import annotations

import argparse
import importlib
import platform
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.tracer_context import dataset, pipeline  # noqa: E402
from src.tracer_context.aacr import file_sha256, load_entries, select_entries  # noqa: E402

OUTPUT_NAME = pipeline.OUTPUT_FILE


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="Dataset folder (created if missing), e.g. data/aacr")
    parser.add_argument("--config", type=Path, action="append",
                        help="Level config, repeatable, parents first (default: configs/levels/*.yaml, sorted)")
    parser.add_argument("--entries", help="Only these entry IDs (owner__repo__number) or 0-based positions, comma-separated")
    parser.add_argument("--languages", help="Only these main languages, comma-separated (e.g. Go,TypeScript)")
    parser.add_argument("--limit", type=int, help="At most this many of the selected entries")
    parser.add_argument("--retries", type=int, default=2, help="Retries per failed PR within a run (default 2)")
    parser.add_argument("--retry-wait", type=float, default=30, help="Seconds before the first retry, doubling (default 30)")
    parser.add_argument("--min-free-gb", type=float, default=10, help="Stop cleanly below this much free disk (default 10)")
    parser.add_argument("--tool", type=Path, help="codebase-memory-mcp binary (default: the configs' path, else download)")
    parser.add_argument("--keep-repos", action="store_true", help="Keep repository clones after their PRs")
    parser.add_argument("--force", action="store_true", help="Rebuild PRs even when already built")
    parser.add_argument("--offline", action="store_true", help="Never download the dataset file, tool or tokenizer")
    parser.add_argument("--dry-run", action="store_true", help="Run the setup checks and show the plan, build nothing")
    parser.add_argument("--fetch-depth", type=int, default=50, help="Initial git history depth (default 50)")
    parser.add_argument("--max-depth", type=int, default=3200, help="Give up finding a merge base beyond this")
    parser.add_argument("--verbose", action="store_true", help="Also print debug lines (always in the .log file)")
    return parser.parse_args()


def _raise_interrupt(signum, frame):  # SIGTERM/SIGHUP behave like Ctrl+C so cleanup runs
    raise KeyboardInterrupt


def setup(args, root: Path, log) -> dict:
    """Run every setup check; log each result; raise SetupError listing all failures."""
    allow_download = not args.offline
    failures, state = [], {}

    def check(name, func):
        try:
            result = func()
            log.info(f"  ok    {name}{f': {result}' if isinstance(result, str) and result else ''}")
            return result
        except pipeline.SetupError as error:
            failures.append(f"{name}: {error}")
            log.error(f"  FAIL  {name}: {error}")
        except (SystemExit, Exception) as error:  # noqa: BLE001 - any setup problem is reported, not a crash
            failures.append(f"{name}: {type(error).__name__}: {error}")
            log.error(f"  FAIL  {name}: {type(error).__name__}: {error}")
            log.debug(f"{name} failure details", exc_info=True)
        return None

    log.info("setup checks:")
    check("git", lambda: pipeline.require_command("git", "fetch repositories"))
    check("gh", lambda: pipeline.require_command("gh", "fetch PR titles and descriptions"))
    check("gh login", pipeline.require_gh_login)
    for module in ("yaml", "transformers", "huggingface_hub"):
        check(f"python package {module}", lambda m=module: f"{importlib.import_module(m).__name__}")
    state["dataset"] = check("dataset file", lambda: pipeline.ensure_dataset_file(root, log, allow_download))
    if state["dataset"]:
        log.info(f"        {state['dataset']} (sha256 {file_sha256(state['dataset'])[:12]})")

    from src.tracer_context.runtime import load_level_configs  # imports need the packages above

    config_paths = args.config or sorted((PROJECT_ROOT / "configs" / "levels").glob("*.yaml"))
    state["configs"] = check("level configs", lambda: load_level_configs(config_paths))
    if state["configs"]:
        log.info(f"        {len(state['configs'])} levels: {', '.join(state['configs'])}")
        first = next(iter(state["configs"].values()))
        configured = args.tool or Path(first["tracer"]["binary"])
        configured = configured if configured.is_absolute() else PROJECT_ROOT / configured
        state["tool"] = check("codebase-memory-mcp",
                              lambda: pipeline.ensure_tool(configured, root / ".work" / "tools", log, allow_download))
        if state["tool"]:
            log.info(f"        {state['tool']} ({pipeline.tool_version(state['tool'])})")
        for name in sorted({cfg["packing"]["tokenizer"] for cfg in state["configs"].values()}):
            check(f"tokenizer {name}", lambda n=name: pipeline.ensure_tokenizer(n, log, allow_download))
    check("no other tool session", pipeline.require_tool_idle)
    check("free disk", lambda: f"{pipeline.require_free_disk(root, args.min_free_gb):.0f} GB free")
    if failures:
        raise pipeline.SetupError(f"{len(failures)} setup check(s) failed; fix them and re-run")
    return state


def main() -> int:
    args = parse_args()
    root = args.root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    log, log_path = pipeline.setup_logging(root / "logs", args.verbose)
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _raise_interrupt)
    code_commit = subprocess.run(["git", "-C", str(PROJECT_ROOT), "rev-parse", "--short", "HEAD"],
                                 capture_output=True, text=True).stdout.strip()
    log.info(f"build_dataset: root {root}")
    log.info(f"log file {log_path}; code {code_commit or '?'}; Python {platform.python_version()} on "
             f"{platform.system()} {platform.machine()}")
    log.debug(f"arguments: {vars(args)}")
    out_path = root / OUTPUT_NAME
    work = root / ".work"
    started = time.monotonic()
    try:
        pipeline.ensure_ignored(root, PROJECT_ROOT, log)
        with pipeline.BuildLock(root, log):
            state = setup(args, root, log)
            configs = state["configs"]

            from src.tracer_context.runtime import make_counter

            counters = {}
            for cfg in configs.values():
                counters.setdefault(cfg["packing"]["tokenizer"], make_counter(cfg["packing"]["tokenizer"]))
            tokenizers = {name: label for name, (_, label) in counters.items()}
            binary, tool_version = state["tool"], pipeline.tool_version(state["tool"])

            try:
                entries = load_entries(state["dataset"])
                selected = select_entries(entries, ids=args.entries.split(",") if args.entries else None,
                                          languages=args.languages.split(",") if args.languages else None,
                                          limit=args.limit)
            except (ValueError, KeyError) as error:
                raise pipeline.SetupError(f"cannot select entries from {state['dataset']}: {error}") from None
            if not selected:
                raise pipeline.SetupError("no entries match --entries/--languages/--limit")

            def key_of(entry):
                return dataset.build_key(configs, tool_version, tokenizers, entry["record"])

            existing = dataset.load_records(out_path, status=None)
            built = sum(dataset.is_current(existing.get(e["id"]), key_of(e)) for e in selected)
            failed_before = sum(existing.get(e["id"], {}).get("status") == "failed" for e in selected)
            to_build = len(selected) if args.force else len(selected) - built
            log.info(f"plan: {len(selected)} PRs in {len(dataset.group_by_repo(selected))} repositories; "
                     f"{built} already built{' (rebuilding: --force)' if args.force else ''}, "
                     f"{to_build} to build ({failed_before} of them failed before); output {out_path}")
            if args.dry_run:
                log.info("dry run: nothing built")
                return 0

            shutil.rmtree(work / "tmp", ignore_errors=True)  # caches left by a crashed run
            (work / "tmp").mkdir(parents=True, exist_ok=True)
            extra_build = {"dataset_sha256": file_sha256(state["dataset"]), "code_commit": code_commit}

            def process(entry):
                return dataset.process_entry(entry, configs, counters, binary=binary, tool_version=tool_version,
                                             repos_dir=work / "repos", scratch_dir=work / "tmp", key=key_of(entry),
                                             fetch_depth=args.fetch_depth, max_depth=args.max_depth,
                                             extra_build=extra_build)

            def before_repo(name, group):
                free = pipeline.free_gb(root)
                if free < args.min_free_gb:
                    raise dataset.StopBuild(f"only {free:.1f} GB free (minimum {args.min_free_gb:g} GB) before "
                                            f"{name}; free some space and re-run to continue")
                log.debug(f"repository {name}: {len(group)} PR(s) to build, {free:.0f} GB free")

            def on_retry(record, attempt):
                wait = args.retry_wait * 2 ** (attempt - 1)
                log.warning(f"{record['id']} failed at {record.get('failed_stage')} "
                            f"({record.get('error', '')[:200]}); retry {attempt}/{args.retries} in "
                            f"{pipeline.format_duration(wait)}")

            progress = pipeline.Progress(log, len(selected), to_build)
            counts = dataset.run(selected, process, key_of, out_path, work / "repos", keep_repos=args.keep_repos,
                                 force=args.force, report=progress, retries=args.retries,
                                 retry_wait=args.retry_wait, before_repo=before_repo, on_retry=on_retry,
                                 sleep=pipeline.wait_seconds)
            return summarize(log, out_path, selected, counts, started, log_path)
    except pipeline.SetupError as error:
        log.error(f"setup failed: {error}")
        return 1
    except dataset.StopBuild as error:
        log.error(f"stopped: {error}")
        return 3
    except KeyboardInterrupt:
        log.warning(f"interrupted after {pipeline.format_duration(time.monotonic() - started)}; progress is saved in "
                    f"{out_path}. Re-run the same command to continue.")
        return 130
    except Exception:  # noqa: BLE001 - unexpected crash: keep the traceback in the log
        log.exception(f"build crashed; finished PRs are saved in {out_path}. Re-run the same command to continue.")
        return 1
    finally:
        shutil.rmtree(work / "tmp", ignore_errors=True)
        log.info(f"log saved to {log_path}")


def summarize(log, out_path, selected, counts, started, log_path) -> int:
    records = dataset.load_records(out_path, status=None)
    wanted = {e["id"] for e in selected}
    failed = [r for r in records.values() if r["id"] in wanted and r.get("status") != "ok"]
    ok = sum(1 for r in records.values() if r["id"] in wanted and r.get("status") == "ok")
    size = out_path.stat().st_size / 1e6 if out_path.exists() else 0.0
    log.info(f"done in {pipeline.format_duration(time.monotonic() - started)}: this run built {counts.get('ok', 0)}, "
             f"failed {counts.get('failed', 0)}, skipped {counts.get('skipped', 0)}")
    log.info(f"{ok}/{len(selected)} selected PRs are built in {out_path} ({size:.1f} MB)")
    if failed:
        log.error(f"{len(failed)} PR(s) failed (re-run to retry them):")
        for record in failed:
            log.error(f"  {record['id']} ({record.get('language')}) at {record.get('failed_stage')}: "
                      f"{record.get('error', '')[:200]}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
