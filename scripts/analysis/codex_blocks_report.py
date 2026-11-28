#!/usr/bin/env python3
"""Codex per hunk against the project's levels on the blocks that scripts/harness/run_codex_blocks.py
has finished. The hunks were run in a random order, so the finished blocks are a random sample of
all hunks; the block in progress is left out. Calls no model.

    .venv/bin/python scripts/analysis/codex_blocks_report.py
    .venv/bin/python scripts/analysis/codex_blocks_report.py --blocks 2      # the first two blocks only

It reads the saved order and the sessions under ROOT/harness/, the scored run folder
(<run>_codex<suffix>/hunk_table.csv, written by `record_codex.py score`, which holds Codex and the
levels on the same hunks) and the levels' saved reviews for their tokens.

Comments covered are counted per hunk (a hunk's review can cover the reference comments of its
file). A Codex session that did not finish with a review counts as covering nothing. Intervals are
the middle 95% of 10,000 bootstrap draws of repositories (seed 536).
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.review_outcomes.profiles import read_notebook_profile  # noqa: E402

CODEX = "C01_codex_hunk"
# Dollars per million tokens, the price list of scripts/harness/record_codex.py.
PRICES = {"input": 2.00, "cached_input": 0.10, "cache_write": 2.50, "output": 10.00}


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def repository(hunk: str) -> str:
    return hunk.rsplit("__h", 1)[0].rsplit("__", 1)[0]


def finished_hunks(order: dict, sessions: Path, blocks: int | None = None) -> tuple[list[str], int]:
    """The hunks of the leading blocks in which every hunk has a saved session summary, and how
    many blocks that is. `blocks` caps the number of blocks taken."""
    size, hunks, taken = order["block"], [], 0
    for start in range(0, len(order["hunks"]), size):
        block = order["hunks"][start:start + size]
        if not all((sessions / hunk / "run.json").is_file() for hunk in block) or (blocks is not None and taken >= blocks):
            break
        hunks += block
        taken += 1
    return hunks, taken


def cost(prompt: int, output: int, cached: int = 0, written: int = 0) -> float:
    """What the tokens would cost on a paid API: new input, cache writes, cached input and output."""
    return ((prompt - cached - written) * PRICES["input"] + written * PRICES["cache_write"] + cached * PRICES["cached_input"]
            + output * PRICES["output"]) / 1_000_000


def bootstrap(hunks: list[str], values: dict[str, float], base: dict[str, float] | None = None, draws: int = 10000,
              seed: int = 536) -> tuple[float, float, float]:
    """The sum of `values` over the hunks (divided by the sum of `base` when given), with the
    middle 95% of bootstrap draws of repositories."""
    import numpy as np
    names = sorted({repository(h) for h in hunks})
    index = {name: i for i, name in enumerate(names)}
    top, bottom = np.zeros(len(names)), np.zeros(len(names))
    for hunk in hunks:
        top[index[repository(hunk)]] += values[hunk]
        bottom[index[repository(hunk)]] += base[hunk] if base else 0
    picks = np.random.default_rng(seed).integers(0, len(names), size=(draws, len(names)))
    if base:
        drawn = top[picks].sum(1) / np.maximum(bottom[picks].sum(1), 1e-9)
        point = top.sum() / max(bottom.sum(), 1e-9)
    else:
        drawn, point = top[picks].sum(1), top.sum()
    return float(point), float(np.percentile(drawn, 2.5)), float(np.percentile(drawn, 97.5))


def build_report(profile: str, thinking: str, seed: int, blocks: int | None = None) -> dict:
    config = read_notebook_profile(profile)
    root = (PROJECT_ROOT / config["contexts"]).parent
    suffix = "" if thinking == "on" else "_nothink"
    sessions = root / "harness" / f"codex{suffix}"
    run = PROJECT_ROOT / config["out_root"] / config["run_name"]
    scored = PROJECT_ROOT / config["out_root"] / f"{config['run_name']}_codex{suffix}"
    levels = list(config["levels"])
    order = json.loads((root / "harness" / f"codex_order_seed{seed}.json").read_text(encoding="utf-8"))
    hunks, taken = finished_hunks(order, sessions, blocks)
    if not hunks:
        return {"blocks": 0, "hunks": 0}

    runs = {hunk: json.loads((sessions / hunk / "run.json").read_text(encoding="utf-8")) for hunk in hunks}
    wanted = set(hunks)
    covered: dict[str, dict[str, float]] = defaultdict(dict)       # level -> hunk -> comments covered
    counts: dict[str, Counter] = defaultdict(Counter)              # level -> findings, matched, off the hunk
    on_hunk = {}
    for row in read_csv(scored / "hunk_table.csv"):
        if row["entry"] in wanted:
            covered[row["level"]][row["entry"]] = float(row["references_covered"] or 0)
            counts[row["level"]].update(findings=int(row["findings"] or 0), matched=int(row["findings_matched"] or 0),
                                        off_hunk=int(row["findings_off_hunk"] or 0))
            on_hunk[row["entry"]] = int(row["references_on_hunk"] or 0)
    not_scored = sorted(h for h in hunks if runs[h].get("status") == "ok" and h not in covered[CODEX])
    for hunk in hunks:                                             # a session without a review covers nothing
        covered[CODEX].setdefault(hunk, 0.0)
    missing = [level for level in levels if set(covered[level]) != wanted]
    if missing:
        raise SystemExit(f"{scored / 'hunk_table.csv'} lacks {missing} on some of the {len(hunks)} hunks; run `record_codex.py score`")

    tokens = {}
    usage = [runs[h].get("usage") or {} for h in hunks]
    prompt, output = sum(u.get("input_tokens", 0) for u in usage), sum(u.get("output_tokens", 0) for u in usage)
    cached, written = sum(u.get("cached_input_tokens", 0) for u in usage), sum(u.get("cache_write_input_tokens", 0) for u in usage)
    tokens[CODEX] = {"requests": sum(r.get("requests") or 0 for r in runs.values()), "prompt_tokens": prompt, "output_tokens": output,
                     "new_input_tokens": prompt - cached, "cost_usd": cost(prompt, output, cached, written)}
    for level in levels:
        replies = [json.loads((run / "reviews" / hunk / f"{level}.json").read_text(encoding="utf-8"))["reply"] for hunk in hunks]
        prompt, output = sum(r["prompt_tokens"] for r in replies), sum(r["completion_tokens"] for r in replies)
        tokens[level] = {"requests": len(replies), "prompt_tokens": prompt, "output_tokens": output, "new_input_tokens": prompt,
                         "cost_usd": cost(prompt, output)}

    report = {"profile": profile, "thinking": thinking, "seed": seed, "blocks": taken, "hunks": len(hunks),
              "prs": len({h.rsplit("__h", 1)[0] for h in hunks}), "repositories": len({repository(h) for h in hunks}),
              "references_on_the_hunks": sum(on_hunk.get(h, 0) for h in hunks),
              "sessions": dict(Counter(r.get("status") or "failed" for r in runs.values())), "ok_sessions_not_scored": not_scored,
              "session_seconds": {"median": statistics.median(r["seconds"] for r in runs.values()),
                                  "mean": statistics.fmean(r["seconds"] for r in runs.values())},
              "commands_outside_the_checkout": sum(r.get("commands_outside") or 0 for r in runs.values()), "levels": {}}
    for level in [CODEX, *levels]:
        entry = {"covered": int(sum(covered[level][h] for h in hunks)), **counts[level], **tokens[level],
                 "precision": counts[level]["matched"] / counts[level]["findings"] if counts[level]["findings"] else None,
                 "cost_times_first_level": None}
        if level != CODEX:
            difference = {h: covered[CODEX][h] - covered[level][h] for h in hunks}
            entry["codex_minus_level"] = bootstrap(hunks, difference)
            entry["codex_minus_level_share"] = bootstrap(hunks, difference, covered[level])
            entry["codex_covers_more_on"] = sum(v > 0 for v in difference.values())
            entry["codex_covers_fewer_on"] = sum(v < 0 for v in difference.values())
        report["levels"][level] = entry
    first = report["levels"][levels[0]]["cost_usd"]
    for entry in report["levels"].values():
        entry["cost_times_first_level"] = entry["cost_usd"] / first
    return report


def print_report(report: dict) -> None:
    if not report["hunks"]:
        print("no block is finished yet")
        return
    print(f"{report['blocks']} finished blocks: {report['hunks']} hunks in {report['prs']} PRs and {report['repositories']} repositories "
          f"(a random sample; seed {report['seed']}); thinking {report['thinking']}")
    print(f"Codex sessions: {report['sessions']}; seconds per session: median {report['session_seconds']['median']:.0f}, "
          f"mean {report['session_seconds']['mean']:.0f}; commands that named a path outside the checkout: "
          f"{report['commands_outside_the_checkout']}")
    if report["ok_sessions_not_scored"]:
        print(f"  not scored yet (run `record_codex.py score`): {len(report['ok_sessions_not_scored'])} sessions")
    print(f"reference comments on the sampled hunks' lines: {report['references_on_the_hunks']}")
    print(f"{'':16s}{'covered':>8s}{'findings':>9s}{'precision':>10s}{'off hunk':>9s}{'requests':>9s}{'input tokens':>14s}{'new input':>12s}"
          f"{'output':>10s}{'cost':>9s}{'x first':>8s}")
    for level, e in report["levels"].items():
        precision = f"{e['precision']:.3f}" if e["precision"] is not None else "-"
        print(f"{level:16s}{e['covered']:8d}{e['findings']:9d}{precision:>10s}{e['off_hunk']:9d}{e['requests']:9,d}{e['prompt_tokens']:14,d}"
              f"{e['new_input_tokens']:12,d}{e['output_tokens']:10,d}{e['cost_usd']:9.2f}{e['cost_times_first_level']:8.1f}")
    print("Codex minus each level, comments covered [95% interval]; as a share of the level's count; hunks where Codex covers more, fewer")
    for level, e in report["levels"].items():
        if "codex_minus_level" in e:
            d, s = e["codex_minus_level"], e["codex_minus_level_share"]
            print(f"  {level:14s} {d[0]:+.0f} [{d[1]:+.0f}, {d[2]:+.0f}]; {100 * s[0]:+.0f}% [{100 * s[1]:+.0f}%, {100 * s[2]:+.0f}%]; "
                  f"more on {e['codex_covers_more_on']}, fewer on {e['codex_covers_fewer_on']}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--profile", default="cloud-hunk-kept", help="the per-hunk notebook profile of the run")
    parser.add_argument("--thinking", choices=("on", "off"), default="off")
    parser.add_argument("--seed", type=int, default=536, help="the seed of the saved order")
    parser.add_argument("--blocks", type=int, help="take only this many finished blocks")
    parser.add_argument("--json", type=Path, help="also write the report to this file")
    args = parser.parse_args(argv)
    report = build_report(args.profile, args.thinking, args.seed, args.blocks)
    print_report(report)
    if args.json:
        args.json.write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
