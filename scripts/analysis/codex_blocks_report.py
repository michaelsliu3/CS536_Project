#!/usr/bin/env python3
"""Codex per hunk against the project's levels on the blocks that scripts/harness/run_codex_blocks.py
has finished. The hunks were run in a random order, so the finished blocks are a random sample of
all hunks; the block in progress is left out. Calls no model.

    .venv/bin/python scripts/analysis/codex_blocks_report.py
    .venv/bin/python scripts/analysis/codex_blocks_report.py --blocks 2      # the first two blocks only
    .venv/bin/python scripts/analysis/codex_blocks_report.py --breakdown     # and where Codex covers more or fewer

It reads the saved order and the sessions under ROOT/harness/, the scored run folder
(<run>_codex<suffix>/hunk_table.csv, written by `record_codex.py score`, which holds Codex and the
levels on the same hunks) and the levels' saved reviews for their tokens.

Comments covered are counted per hunk (a hunk's review can cover the reference comments of its
file). A Codex session that did not finish with a review counts as covering nothing. Intervals are
the middle 95% of 10,000 bootstrap draws of repositories (seed 536).

--breakdown adds where Codex covers more and where fewer: the distinct reference comments that the
sampled hunks' reviews cover, by the benchmark's context label, by who wrote the comment and by
severity, and the per-hunk counts by the hunk's size. The labels and sizes come from the selectors'
input file (data/aacr_hunk_selector_files/hunk_outcomes.json).
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
OUTCOMES = PROJECT_ROOT / "data/aacr_hunk_selector_files/hunk_outcomes.json"
TIERS = {"blocking": "blocking", "should_fix": "should fix", "nit": "nit"}


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


def grouped_difference(group: list[str], unit: list[str], first: list[float], second: list[float], draws: int = 10000,
                       seed: int = 536) -> dict[str, dict]:
    """Per group: its items, the sums of `first` and `second`, and first minus second with the
    middle 95% of bootstrap draws of the units (repositories)."""
    import numpy as np
    names = sorted(set(unit))
    index = {name: i for i, name in enumerate(names)}
    picks = np.random.default_rng(seed).integers(0, len(names), size=(draws, len(names)))
    out = {}
    for key in sorted(set(group)):
        per_unit, a, b, items = np.zeros(len(names)), 0.0, 0.0, 0
        for g, u, x, y in zip(group, unit, first, second):
            if g == key:
                per_unit[index[u]] += x - y
                a, b, items = a + x, b + y, items + 1
        drawn = per_unit[picks].sum(1)
        out[key] = {"items": items, "first": a, "second": b,
                    "difference": [a - b, float(np.percentile(drawn, 2.5)), float(np.percentile(drawn, 97.5))]}
    return out


def size_group(lines: int, edges: tuple[int, int]) -> str:
    """The hunk's size group by its changed lines; `edges` are the last sizes of the first two groups."""
    low, middle = edges
    if lines <= low:
        return f"1. up to {low} changed lines"
    return f"2. {low + 1} to {middle} changed lines" if lines <= middle else f"3. over {middle} changed lines"


def build_breakdown(profile: str, thinking: str, seed: int, blocks: int | None = None, outcomes: Path = OUTCOMES) -> dict:
    """Where Codex covers more and where fewer than each level, on the finished blocks."""
    import numpy as np
    config = read_notebook_profile(profile)
    root = (PROJECT_ROOT / config["contexts"]).parent
    suffix = "" if thinking == "on" else "_nothink"
    run = PROJECT_ROOT / config["out_root"] / config["run_name"]
    scored = PROJECT_ROOT / config["out_root"] / f"{config['run_name']}_codex{suffix}"
    levels = list(config["levels"])
    order = json.loads((root / "harness" / f"codex_order_seed{seed}.json").read_text(encoding="utf-8"))
    hunks, taken = finished_hunks(order, root / "harness" / f"codex{suffix}", blocks)
    saved = json.loads(Path(outcomes).read_text(encoding="utf-8"))
    lines = {row["hunk"]: row["added"] + row["removed"] for row in saved["rows"]}

    def covered(hunk: str, level: str) -> list[int]:
        path = (scored if level == CODEX else run) / "judgments" / hunk / f"{level}.json"
        return json.loads(path.read_text(encoding="utf-8"))["covered_indices"] if path.is_file() else []   # no review: nothing covered

    cells = {level: {hunk: covered(hunk, level) for hunk in hunks} for level in [CODEX, *levels]}
    found = {level: {(hunk.rsplit("__h", 1)[0], index) for hunk, items in cells[level].items() for index in items} for level in cells}
    references = sorted(set().union(*found.values()))
    labels = [saved["refs"][pr][str(index)] for pr, index in references]
    groupings = {"context label": [label.get("context") or "no label" for label in labels],
                 "author": ["written by a person" if label.get("human") else "generated by an AI tool" for label in labels],
                 "severity": [TIERS.get(label.get("tier"), "no label") for label in labels]}
    repos = [pr.rsplit("__", 1)[0] for pr, _ in references]
    report = {"blocks": taken, "hunks": len(hunks), "references_covered_by_any": len(references), "references": {}, "hunk_size": {}}
    for name, group in groupings.items():
        report["references"][name] = {level: grouped_difference(group, repos, [float(r in found[CODEX]) for r in references],
                                                                 [float(r in found[level]) for r in references]) for level in levels}
    sizes = [lines[hunk] for hunk in hunks]
    edges = tuple(int(v) for v in np.quantile(sizes, [1 / 3, 2 / 3]))
    report["size_edges"] = list(edges)
    for level in levels:
        report["hunk_size"][level] = grouped_difference([size_group(lines[h], edges) for h in hunks], [repository(h) for h in hunks],
                                                        [float(len(cells[CODEX][h])) for h in hunks], [float(len(cells[level][h])) for h in hunks])
    return report


def print_breakdown(report: dict) -> None:
    levels = list(next(iter(report["references"].values())))
    print(f"\nWhere Codex covers more or fewer ({report['hunks']} hunks; {report['references_covered_by_any']} distinct reference comments "
          f"covered by some reviewer). Codex, then each level's count and Codex minus it [95% interval]")
    tables = {**{f"reference comments by {name}": table for name, table in report["references"].items()},
              "per-hunk counts by the hunk's size": report["hunk_size"]}
    for title, table in tables.items():
        print(f"  {title}")
        for key in table[levels[0]]:
            first = table[levels[0]][key]
            cells = "; ".join(f"{level} {table[level][key]['second']:.0f} ({table[level][key]['difference'][0]:+.0f} "
                              f"[{table[level][key]['difference'][1]:+.0f}, {table[level][key]['difference'][2]:+.0f}])" for level in levels)
            print(f"    {key:28s} items {first['items']:4d}  Codex {first['first']:4.0f} | {cells}")


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
    # The scored table holds only hunks Codex reviewed. A session that ended without a review (it ran
    # out of time twice) still counts: the levels' rows for its hunk are the same in the run's own table.
    no_review = {h for h in hunks if runs[h].get("status") != "ok" and h not in covered[levels[0]]}
    if no_review:
        for row in read_csv(run / "hunk_table.csv"):
            if row["entry"] in no_review and row["level"] in levels:
                covered[row["level"]][row["entry"]] = float(row["references_covered"] or 0)
                counts[row["level"]].update(findings=int(row["findings"] or 0), matched=int(row["findings_matched"] or 0),
                                            off_hunk=int(row["findings_off_hunk"] or 0))
                on_hunk[row["entry"]] = int(row["references_on_hunk"] or 0)
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
    parser.add_argument("--breakdown", action="store_true", help="also show where Codex covers more and where fewer")
    args = parser.parse_args(argv)
    report = build_report(args.profile, args.thinking, args.seed, args.blocks)
    print_report(report)
    if args.breakdown and report["hunks"]:
        report["breakdown"] = build_breakdown(args.profile, args.thinking, args.seed, args.blocks)
        print_breakdown(report["breakdown"])
    if args.json:
        args.json.write_text(json.dumps(report, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
