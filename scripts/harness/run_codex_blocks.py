#!/usr/bin/env python3
"""Run Codex per hunk on a profile's hunks in a random order, block by block, so that the run
can be stopped at any time and the hunks done so far are a random sample of all of them.

    .venv/bin/python scripts/harness/run_codex_blocks.py --profile cloud-hunk-kept --thinking off
    .venv/bin/python scripts/harness/run_codex_blocks.py --profile cloud-hunk-kept --thinking off --dry-run

scripts/harness/record_codex.py runs one repository after another, so a run that is stopped early
has reviewed whole repositories and none of the others. This script fixes the order first:

- the hunks are those the profile's own run reviewed at every level (so each can be compared with
  every level), sorted and then shuffled once with --seed. The order is saved under
  ROOT/harness/ and read back on every later start, so a restart continues the same order;
- they are run in blocks of --block hunks with `record_codex.py hunks --hunks ...`. A session that
  did not finish with a review is tried once more at the end of its block, and a failed one of a
  finished block once more at every later start;
- after each block `record_codex.py score` judges the new reviews, so what has finished is always
  scored (needs the judge's server, which is the same server).

Every finished block is a random sample without replacement of the hunks not run before it, and
the finished blocks together are a random sample of all hunks. The block in progress when the run
stops is not: within a block the sessions run repository by repository.

Progress is written to ROOT/harness/codex_blocks<suffix>.json after every block. Sessions are saved
one folder per hunk by record_codex.py, which skips what is already done, so this script can be
stopped and started at any time.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import random
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request
from collections import Counter
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.review_outcomes.profiles import read_notebook_profile  # noqa: E402

RECORD = Path(__file__).resolve().parent / "record_codex.py"
SETTINGS = PROJECT_ROOT / "configs" / "codex_harness" / "config.toml"


def reviewed_hunks(run: Path, levels: list[str]) -> list[str]:
    """The hunks with a review at every level in the run's hunk_table.csv, sorted."""
    statuses: dict[str, dict] = {}
    with (run / "hunk_table.csv").open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            statuses.setdefault(row["entry"], {})[row["level"]] = row.get("review_status") or "ok"
    return sorted(hunk for hunk, cells in statuses.items() if all(cells.get(level) == "ok" for level in levels))


def random_order(hunks: list[str], seed: int) -> list[str]:
    order = sorted(hunks)
    random.Random(seed).shuffle(order)
    return order


def load_order(path: Path, hunks: list[str], seed: int, block: int) -> dict:
    """The saved order, or a new one written to `path`. A saved order must hold the same hunks."""
    if path.is_file():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if sorted(saved["hunks"]) != sorted(hunks):
            raise SystemExit(f"{path} holds another set of hunks ({len(saved['hunks'])} against {len(hunks)}); "
                             "move it away to draw a new order")
        return saved
    order = {"seed": seed, "block": block, "made": datetime.now().isoformat(timespec="seconds"), "hunks": random_order(hunks, seed)}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(order, indent=1), encoding="utf-8")
    return order


def blocks_of(order: list[str], size: int) -> list[list[str]]:
    return [order[i:i + size] for i in range(0, len(order), size)]


def session_status(sessions: Path, hunk: str) -> str:
    """`ok`, the status of a session that did not finish with a review, or `not run`."""
    path = sessions / hunk / "run.json"
    if not path.is_file():
        return "not run"
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("status") or "failed"
    except json.JSONDecodeError:
        return "failed"


def server_answers(base_url: str) -> bool:
    try:
        with urllib.request.urlopen(f"{base_url}/models", timeout=10) as reply:
            return reply.status == 200
    except (urllib.error.URLError, OSError):
        return False


def wait_for_server(base_url: str) -> None:
    waited = False
    while not server_answers(base_url):
        if not waited:
            print(f"=== waiting for the model server at {base_url} {datetime.now():%Y-%m-%d %H:%M:%S}", flush=True)
            waited = True
        time.sleep(30)
    if waited:
        print(f"=== the model server answers again {datetime.now():%Y-%m-%d %H:%M:%S}", flush=True)


def run_step(arguments: list[str]) -> int:
    print("+ record_codex.py " + " ".join(arguments[:12]) + (" ..." if len(arguments) > 12 else ""), flush=True)
    return subprocess.run([sys.executable, str(RECORD), *arguments], cwd=PROJECT_ROOT).returncode


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default=os.environ.get("OUTCOME_PROFILE", "cloud-hunk-kept"), help="a per-hunk notebook profile")
    parser.add_argument("--thinking", choices=("on", "off"), default="off", help="the model's thinking in Codex's sessions (default off)")
    parser.add_argument("--seed", type=int, default=536, help="the seed of the random order (default 536)")
    parser.add_argument("--block", type=int, default=100, help="hunks per block (default 100)")
    parser.add_argument("--max-blocks", type=int, help="stop after this many blocks of this start")
    parser.add_argument("--parallel", type=int, default=6, help="repositories at a time (default 6)")
    parser.add_argument("--concurrency", type=int, default=2, help="sessions at a time within one PR (default 2)")
    parser.add_argument("--timeout", type=int, default=900, help="seconds before a session is stopped (default 900)")
    parser.add_argument("--base-url", help="the model server, when it is not at the address in the settings file")
    parser.add_argument("--no-score", action="store_true", help="do not judge after each block")
    parser.add_argument("--dry-run", action="store_true", help="show the order and what is done; run nothing")
    args = parser.parse_args(argv)

    config = read_notebook_profile(args.profile)
    root = (PROJECT_ROOT / config["contexts"]).parent
    suffix = "" if args.thinking == "on" else "_nothink"
    sessions = root / "harness" / f"codex{suffix}"
    settings = tomllib.loads(SETTINGS.read_text(encoding="utf-8"))
    base_url = (args.base_url or settings["model_providers"][settings["model_provider"]]["base_url"]).rstrip("/")
    hunks = reviewed_hunks(PROJECT_ROOT / config["out_root"] / config["run_name"], config["levels"])
    order = load_order(root / "harness" / f"codex_order_seed{args.seed}.json", hunks, args.seed, args.block)
    blocks = blocks_of(order["hunks"], order["block"])
    progress_path = root / "harness" / f"codex_blocks{suffix}.json"
    common = ["--profile", args.profile, "--thinking", args.thinking] + (["--base-url", args.base_url] if args.base_url else [])

    def statuses(block: list[str]) -> dict[str, str]:
        return {hunk: session_status(sessions, hunk) for hunk in block}

    def save_progress() -> dict:
        done = [statuses(block) for block in blocks]
        progress = {"profile": args.profile, "thinking": args.thinking, "seed": order["seed"], "block": order["block"],
                    "hunks": len(order["hunks"]), "updated": datetime.now().isoformat(timespec="seconds"),
                    "blocks": [{"index": i + 1, "hunks": block, "status": dict(Counter(s.values())),
                                "complete": all(v != "not run" for v in s.values())}
                               for i, (block, s) in enumerate(zip(blocks, done))]}
        progress["sessions"] = dict(Counter(v for s in done for v in s.values()))
        progress_path.write_text(json.dumps(progress, indent=1), encoding="utf-8")
        return progress

    progress = save_progress()
    print(f"{len(order['hunks'])} hunks in {len(blocks)} blocks of {order['block']} (seed {order['seed']}); sessions so far: "
          f"{progress['sessions']}; order in {root / 'harness' / f'codex_order_seed{args.seed}.json'}", flush=True)
    if args.dry_run:
        for entry in progress["blocks"][:5]:
            print(f"  block {entry['index']}: {entry['status']}; first hunks {entry['hunks'][:3]}")
        print("nothing was run")
        return 0

    # A session that failed in a block an earlier start finished (for example because the server went
    # away as the run was stopped) gets another try now; one that ran out of time does not.
    failed = [hunk for block in blocks if all(v != "not run" for v in statuses(block).values())
              for hunk, status in statuses(block).items() if status == "failed"]
    if failed:
        print(f"\n=== trying again {len(failed)} sessions that failed in finished blocks {datetime.now():%Y-%m-%d %H:%M:%S}", flush=True)
        wait_for_server(base_url)
        run_step(["hunks", *common, "--keep-repos", "--parallel", str(args.parallel), "--concurrency", str(args.concurrency),
                  "--timeout", str(args.timeout), "--hunks", *failed])

    ran = 0
    for index, block in enumerate(blocks, 1):
        if all(status != "not run" for status in statuses(block).values()):
            continue                                              # every hunk was tried: an earlier start finished this block
        if args.max_blocks is not None and ran >= args.max_blocks:
            break
        ran += 1
        print(f"\n=== block {index} of {len(blocks)} started {datetime.now():%Y-%m-%d %H:%M:%S}", flush=True)
        for attempt in (1, 2):                                    # a session without a review gets one more try
            todo = [hunk for hunk, status in statuses(block).items() if status != "ok"]
            if not todo:
                break
            wait_for_server(base_url)
            run_step(["hunks", *common, "--keep-repos", "--parallel", str(args.parallel), "--concurrency", str(args.concurrency),
                      "--timeout", str(args.timeout), "--hunks", *todo])
            if not server_answers(base_url):                      # the server went away mid-block: wait, then do the block again
                wait_for_server(base_url)
                run_step(["hunks", *common, "--keep-repos", "--parallel", str(args.parallel), "--concurrency", str(args.concurrency),
                          "--timeout", str(args.timeout), "--hunks", *[h for h, s in statuses(block).items() if s != "ok"]])
        progress = save_progress()
        print(f"=== block {index} of {len(blocks)} finished {datetime.now():%Y-%m-%d %H:%M:%S}: {progress['blocks'][index - 1]['status']}; "
              f"all sessions {progress['sessions']}", flush=True)
        if not args.no_score:
            wait_for_server(base_url)
            run_step(["score", *common])
            print(f"=== block {index} of {len(blocks)} scored {datetime.now():%Y-%m-%d %H:%M:%S}", flush=True)
    progress = save_progress()
    left = sum(not entry["complete"] for entry in progress["blocks"])
    print(f"\n=== {'all blocks done' if not left else f'stopped with {left} blocks left'} {datetime.now():%Y-%m-%d %H:%M:%S}: "
          f"{progress['sessions']}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
