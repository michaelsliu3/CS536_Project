"""Delete a context-dataset build and downloaded repositories, so the next build starts over.

    .venv/bin/python scripts/dataset/clean_dataset.py ROOT [--logs | --all] [--yes | --dry-run]

By default it deletes:
  - the built records (ROOT/context_levels.jsonl.gz) and the clones and tool caches in ROOT/.work;
  - the repositories the inspection runner (scripts/tracer/run_tracer_batch.py) cloned into
    data/aacr_tracer/repos (another folder with --runner-work). Its saved results stay.
It keeps ROOT/positive_samples.json, ROOT/logs and a downloaded tool, so the restart needs no
downloads.
  --logs     also delete the build logs (ROOT/logs/build_*.log)
  --all      delete everything the builders wrote: in ROOT, including the AACR file and the
             tool, and in the runner's folder, including its saved results (entries/,
             summary.csv) and outputs older defaults left there
  --dry-run  only list what would be deleted
  --yes      do not ask before deleting

It lists every path with its size and asks first. It refuses while a build runs on ROOT or the
inspection runner runs, and only touches the builders' own files: a ROOT with none of them is
left alone.

Exit codes: 0 cleaned (or nothing to do, or --dry-run); 1 refused, declined or failed.
"""
from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.tracer_context import pipeline  # noqa: E402


def human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size} B"


def parse_args():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("root", type=Path, help="Dataset folder given to build_dataset.py, e.g. data/aacr")
    parser.add_argument("--runner-work", type=Path, default=PROJECT_ROOT / pipeline.RUNNER_WORK,
                        help="The inspection runner's --work folder: its repos/ clones are deleted, and with "
                             "--all everything it wrote "
                             f"(default {pipeline.RUNNER_WORK})")
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--logs", action="store_true", help="Also delete the build logs")
    scope.add_argument("--all", action="store_true", help="Delete everything the builder wrote in ROOT")
    answer = parser.add_mutually_exclusive_group()
    answer.add_argument("--yes", action="store_true", help="Delete without asking")
    answer.add_argument("--dry-run", action="store_true", help="Only list what would be deleted")
    return parser.parse_args()


def show(title: str, base: Path, paths: list[Path], sizes: list[int]) -> None:
    print(f"{title} {base}:")
    for path, size in zip(paths, sizes):
        print(f"  {human(size):>9}  {path.relative_to(base)}{'/' if path.is_dir() else ''}")


def main() -> int:
    args = parse_args()
    logging.basicConfig(format="%(levelname)s %(message)s", level=logging.INFO)
    log = logging.getLogger("clean_dataset")
    root = args.root.expanduser().resolve()
    runner_work = args.runner_work.expanduser().resolve()
    try:
        is_build = root.exists()
        if is_build:
            try:
                pipeline.clean_targets(root, logs=args.logs, everything=args.all)  # refuses a folder that is not a build
            except pipeline.SetupError:
                if not pipeline.runner_targets(runner_work, everything=args.all):
                    raise
                log.info(f"{root} has no dataset build files; only the inspection runner's files are deleted")
                is_build = False
        # No build may start or run on ROOT while cleaning it.
        with pipeline.BuildLock(root, log) if is_build else contextlib.nullcontext():
            targets = pipeline.clean_targets(root, logs=args.logs, everything=args.all) if is_build else []
            runner = pipeline.runner_targets(runner_work, everything=args.all)
            if runner and not args.dry_run:
                pipeline.require_runner_idle()
            if not targets and not runner:
                log.info("nothing to delete (kept: the AACR file, logs, downloaded tool and the runner's results; "
                         "see --logs and --all)")
                return 0
            sizes = [pipeline.path_size(path) for path in targets]
            runner_sizes = [pipeline.path_size(path) for path in runner]
            verb = "would delete" if args.dry_run else "to delete"
            if targets:
                show(f"{verb} in", root, targets, sizes)
            if runner and args.all:
                show(f"{verb}: inspection runner output in", runner_work, runner, runner_sizes)
            elif runner:
                show(f"{verb}: inspection runner clones in", runner_work / "repos", runner, runner_sizes)
            total = sum(sizes) + sum(runner_sizes)
            print(f"  {human(total):>9}  total")
            if args.dry_run:
                return 0
            if not args.yes:
                if not sys.stdin.isatty():
                    log.error("not deleting: no terminal to ask; pass --yes to delete without asking")
                    return 1
                if input("Delete these? [y/N] ").strip().lower() not in ("y", "yes"):
                    log.info("nothing deleted")
                    return 1
            if targets:
                pipeline.remove_paths(root, targets)
            for path in runner:
                pipeline.delete_path(path)
            for folder in [runner_work / "repos"] + ([runner_work] if args.all else []):
                with contextlib.suppress(OSError):
                    folder.rmdir()  # only when empty
        if args.all and is_build:
            with contextlib.suppress(OSError):
                root.rmdir()  # empty once the lock is gone
        log.info(f"deleted {len(targets) + len(runner)} paths ({human(total)})"
                 + (f"; the next build_dataset.py run on {root} starts from scratch" if targets else ""))
        return 0
    except pipeline.SetupError as error:
        log.error(str(error))
        return 1
    except KeyboardInterrupt:
        log.error("interrupted; some paths may already be deleted (run again to finish)")
        return 1
    except OSError as error:
        log.error(f"could not delete: {error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
