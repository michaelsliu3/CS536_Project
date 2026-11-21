#!/usr/bin/env python3
"""Turn ContextCRBench into a dataset folder the builder can read (src/tracer_context/ccrb.py).

    .venv/bin/python scripts/dataset/contextcrbench.py threads data/ccrb_hunk
    .venv/bin/python scripts/dataset/contextcrbench.py lookup  data/ccrb_hunk --candidates 12
    .venv/bin/python scripts/dataset/contextcrbench.py write   data/ccrb_hunk --per-language 3
    .venv/bin/python scripts/dataset/build_dataset.py data/ccrb_hunk --unit hunk
    .venv/bin/python scripts/dataset/contextcrbench.py check   data/ccrb_hunk

threads  one row per comment thread of every record -> ROOT/work/threads.jsonl
lookup   draws candidate PRs (seeded) and asks GitHub for each one's review comments, the PR and
         the commits the comments were made on -> ROOT/work/github/<PR id>.json (asked once)
write    drops what cannot be used, keeps one reviewed commit per PR, picks PRs per language
         -> ROOT/positive_samples.json (AACR's shape), ROOT/work/references.jsonl, selection.json;
         the PRs an earlier write selected stay selected and first, so a larger run reuses them
check    after the build: each reference must land on one hunk, and that hunk must be the code
         the reviewer saw -> ROOT/work/check_report.json

Only `lookup` uses the network (a signed-in `gh`); it reads and never writes on GitHub.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

from src.tracer_context import ccrb  # noqa: E402

SOURCE = PROJECT_ROOT / "data/contextcrbench/extracted/final_json"
AACR = PROJECT_ROOT / "data/aacr/positive_samples.json"
PULL_KEYS = ("title", "created_at", "merged_at", "merged", "state", "changed_files", "additions", "deletions", "commits")
COMMENT_KEYS = ("id", "path", "body", "diff_hunk", "original_commit_id", "commit_id", "side", "start_side", "line",
                "original_line", "start_line", "original_start_line", "created_at", "in_reply_to_id", "author_association")


def read_jsonl(path: Path) -> list[dict]:
    with Path(path).open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    tmp.replace(path)


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    tmp.replace(path)


# ---------- threads ----------

def cmd_threads(args) -> int:
    names = sorted(os.listdir(args.source), key=lambda name: int(name.split(".")[0]))

    def records():
        for count, name in enumerate(names):
            with open(args.source / name, encoding="utf-8") as handle:
                yield json.load(handle)
            if count and count % 10000 == 0:
                print(f"  {count} of {len(names)} records", flush=True)

    threads = ccrb.collect_threads(records())
    out = args.root / "work" / "threads.jsonl"
    write_jsonl(out, threads)
    print(f"{len(names)} records -> {len(threads)} threads in {len({(t['repo'], t['pr']) for t in threads})} PRs, "
          f"{len({t['repo'] for t in threads})} repositories\nwrote {out}")
    return 0


# ---------- lookup ----------

def gh_api(path: str, tries: int = 3) -> tuple[int, object]:
    """(200, parsed JSON), (404, message) when GitHub does not have it; raises on anything else."""
    for attempt in range(tries):
        result = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=180)
        if result.returncode == 0:
            return 200, json.loads(result.stdout)
        error = result.stderr.strip()[:300]
        if "HTTP 404" in error or "HTTP 422" in error:
            return 404, error
        time.sleep(5 * (attempt + 1))
    raise RuntimeError(f"gh api {path} failed: {error}")


def candidates(threads: list[dict], per_language: int, per_repo: int, seed: int, exclude: set[str]) -> list[str]:
    """PR ids to look up: per language, a seeded shuffle of its PRs, at most `per_repo` per repository."""
    by_language: dict[str, set[str]] = defaultdict(set)
    for thread in threads:
        pr = ccrb.pr_id(thread["repo"], thread["pr"])
        if pr not in exclude:
            by_language[thread["language"]].add(pr)
    chosen = []
    for language in sorted(by_language):
        prs = sorted(by_language[language])
        random.Random(f"{seed}:{language}").shuffle(prs)
        taken: Counter = Counter()
        for pr in prs:
            repo = pr.rsplit("__", 1)[0]
            if taken[repo] < per_repo:
                taken[repo] += 1
                chosen.append(pr)
            if sum(taken.values()) >= per_language:
                break
    return chosen


def look_up(repo: str, number: int, threads: list[dict]) -> dict:
    code, pull = gh_api(f"repos/{repo}/pulls/{number}")
    if code != 200:
        return {"repo": repo, "pr": number, "error": "the pull request is not on GitHub"}
    comments, page = [], 1
    while True:
        code, got = gh_api(f"repos/{repo}/pulls/{number}/comments?per_page=100&page={page}")
        if code != 200 or not got:
            break
        comments += got
        if len(got) < 100:
            break
        page += 1
    kept = [{**{key: c.get(key) for key in COMMENT_KEYS},
             "user": {"login": (c.get("user") or {}).get("login"), "type": (c.get("user") or {}).get("type")}} for c in comments]
    base, commits = threads[0]["base_commit"], {}
    for thread in threads:
        comment = ccrb.match_comment(thread, kept)
        sha = comment and comment["original_commit_id"]
        if sha and sha not in commits:
            code, compare = gh_api(f"repos/{repo}/compare/{base}...{sha}?per_page=1")
            commits[sha] = ({"exists": True, "merge_base": (compare.get("merge_base_commit") or {}).get("sha"),
                             "status": compare.get("status"), "ahead_by": compare.get("ahead_by")}
                            if code == 200 else {"exists": False})
    return {"repo": repo, "pr": number,
            "pull": {**{key: pull.get(key) for key in PULL_KEYS}, "user": {"login": (pull.get("user") or {}).get("login")},
                     "base": {"sha": pull["base"]["sha"], "ref": pull["base"]["ref"]}, "head": {"sha": pull["head"]["sha"]}},
            "comments": kept, "commits": commits}


def cmd_lookup(args) -> int:
    threads = read_jsonl(args.root / "work" / "threads.jsonl")
    by_pr: dict[str, list[dict]] = defaultdict(list)
    for thread in threads:
        by_pr[ccrb.pr_id(thread["repo"], thread["pr"])].append(thread)
    exclude = set()
    if args.exclude_dataset.is_file():
        from src.tracer_context.aacr import entry_id
        exclude = {entry_id(record).lower() for record in json.loads(args.exclude_dataset.read_text(encoding="utf-8"))}
    skipped = {pr for pr in by_pr if pr.lower() in exclude}
    chosen = candidates(threads, args.candidates, args.per_repo, args.seed, skipped)
    out_dir = args.root / "work" / "github"
    asked = 0
    for count, pr in enumerate(chosen, 1):
        path = out_dir / f"{pr}.json"
        if path.is_file():
            continue
        first = by_pr[pr][0]
        write_json(path, {"id": pr, **look_up(first["repo"], first["pr"], by_pr[pr])})
        asked += 1
        print(f"  [{count}/{len(chosen)}] {pr}", flush=True)
    write_json(args.root / "work" / "candidates.json",
               {"seed": args.seed, "per_language": args.candidates, "per_repo": args.per_repo,
                "excluded_as_in": str(args.exclude_dataset), "excluded": sorted(skipped), "candidates": chosen})
    print(f"{len(chosen)} candidate PRs ({len(skipped)} left out because they are in {args.exclude_dataset.name}); "
          f"{asked} looked up now, {len(chosen) - asked} already saved in {out_dir}")
    return 0


# ---------- write ----------

def convert_pr(pr: str, threads: list[dict], found: dict, max_files: int, max_lines: int) -> tuple[dict | None, list[dict], Counter]:
    """(dataset entry or None, its reference rows, what was dropped and why) for one looked-up PR."""
    dropped: Counter = Counter()
    if found.get("error"):
        dropped["PR not on GitHub"] += len(threads)
        return None, [], dropped
    pull, author = found["pull"], (found["pull"].get("user") or {}).get("login")
    matched = []
    for thread in threads:
        comment = ccrb.match_comment(thread, found["comments"])
        if comment is None:
            dropped["comment not found on GitHub"] += 1
        elif (comment.get("user") or {}).get("type") == "Bot":
            dropped["comment by a bot"] += 1
        elif (comment.get("user") or {}).get("login") == author:
            dropped["comment by the PR's author"] += 1
        elif not (found["commits"].get(comment["original_commit_id"]) or {}).get("exists"):
            dropped["reviewed commit is gone"] += 1
        else:
            matched.append((thread, comment))
    target = ccrb.choose_commit(matched)
    references, rows = [], []
    for thread, comment in matched:
        if comment["original_commit_id"] != target:
            dropped["made on another commit of the PR"] += 1
            continue
        ref = ccrb.reference(thread, comment)
        if ref is None:
            dropped["commented line not readable from the hunk"] += 1
            continue
        references.append(ref)
        rows.append({"thread": thread["thread"], "pr": pr, "reviewed_commit": target, "path": ref["path"], "side": ref["side"],
                     "from_line": ref["from_line"], "to_line": ref["to_line"], "final_line": thread["final_line"],
                     "made_on_head": target == thread["head_commit"], "hunk_as_reviewed": comment["diff_hunk"]})
    if not references:
        return None, [], dropped
    if (pull.get("changed_files") or 0) > max_files or (pull.get("additions") or 0) + (pull.get("deletions") or 0) > max_lines:
        dropped["PR over the size limit"] += len(references)
        return None, [], dropped
    return ccrb.entry(threads[0], pull, target, references), rows, dropped


def previous_selection(root: Path, fresh: bool) -> list[str]:
    """The PRs the last `write` selected, in its order; [] with `fresh` or before any write."""
    path = root / "work" / "selection.json"
    if fresh or not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8")).get("selected", [])


def cmd_write(args) -> int:
    threads = read_jsonl(args.root / "work" / "threads.jsonl")
    by_pr: dict[str, list[dict]] = defaultdict(list)
    for thread in threads:
        by_pr[ccrb.pr_id(thread["repo"], thread["pr"])].append(thread)
    plan = json.loads((args.root / "work" / "candidates.json").read_text(encoding="utf-8"))
    # The PRs selected before come first, in their old order, and stay selected whatever the
    # quotas: their contexts, labels and reviews may already exist, and their place in the dataset
    # file stays the same. New PRs fill the quotas after them, in candidate order.
    kept = previous_selection(args.root, args.fresh)
    order = kept + [pr for pr in plan["candidates"] if pr not in set(kept)]
    entries, rows, dropped, outcome = [], [], Counter(), {}
    taken_language: Counter = Counter()
    taken_repo: Counter = Counter()
    usable = Counter()
    for pr in order:
        path = args.root / "work" / "github" / f"{pr}.json"
        if not path.is_file():
            outcome[pr] = "not looked up"
            continue
        made, refs, why = convert_pr(pr, by_pr[pr], json.loads(path.read_text(encoding="utf-8")), args.max_files, args.max_lines)
        dropped.update(why)
        usable["threads"] += len(by_pr[pr])
        if made is None:
            outcome[pr] = ("dropped (selected before): " if pr in kept else "dropped: ") + (", ".join(sorted(why)) or "no thread left")
            continue
        usable["PRs usable"] += 1
        usable["threads usable"] += len(refs)
        language, repo = made["project_main_language"], by_pr[pr][0]["repo"]
        if pr not in kept and (taken_language[language] >= args.per_language or taken_repo[repo] >= args.per_repo):
            outcome[pr] = "usable, not needed"
            continue
        taken_language[language] += 1
        taken_repo[repo] += 1
        outcome[pr] = "selected"
        entries.append(made)
        rows += refs
    selected = [p for p, o in outcome.items() if o == "selected"]
    write_json(args.root / "positive_samples.json", entries)
    write_jsonl(args.root / "work" / "references.jsonl", rows)
    write_json(args.root / "work" / "selection.json",
               {"seed": plan["seed"], "per_language": args.per_language, "per_repo": args.per_repo, "max_files": args.max_files,
                "max_lines": args.max_lines, "candidates": len(plan["candidates"]), "kept_from_before": len(kept),
                "counts": dict(usable), "threads_dropped": dict(dropped), "selected": selected, "outcome": outcome})
    print(f"{len(order)} PRs considered with {usable['threads']} threads: {usable['PRs usable']} PRs and "
          f"{usable['threads usable']} threads usable")
    for reason, count in dropped.most_common():
        print(f"  dropped  {count:4d}  {reason}")
    lost = [p for p in kept if outcome.get(p) != "selected"]
    print(f"selected {len(entries)} PRs with {len(rows)} references: {dict(sorted(taken_language.items()))}; "
          f"{len(kept) - len(lost)} of them kept from the last write" + (f", {len(lost)} lost: {', '.join(lost)}" if lost else "")
          + f"\nwrote {args.root / 'positive_samples.json'}")
    return 0


# ---------- check ----------

def built_hunk(text: str, sections: dict) -> tuple[str, list[str]]:
    """(header line, body lines) of the one hunk in a level's diff section."""
    start, end = sections["diff"]
    lines = text[start:end].split("\n")
    first = next(i for i, line in enumerate(lines) if line.startswith("@@ "))
    body = lines[first + 1:]
    while body and body[-1].strip() in ("", "```"):
        body.pop()
    return lines[first], [line.rstrip("\r") for line in body if not line.startswith("\\")]


def line_text(header: str, body: list[str], side: str, number: int) -> str | None:
    """The text of line `number` (on `side`) of a hunk, or None when the hunk does not show it."""
    match = ccrb._HUNK.match(header)
    old, new = int(match.group(1)), int(match.group(3))
    for line in body:
        mark = line[:1]
        if side == "left" and mark != "+" and old == number or side != "left" and mark != "-" and new == number:
            return line[1:]
        old += mark != "+"
        new += mark != "-"
    return None


def cmd_check(args) -> int:
    from src.review_outcomes import table
    from src.tracer_context.dataset import hunk_units, read_records

    entries = {ccrb.pr_id(*_repo_number(e["githubPrUrl"])): e for e in json.loads((args.root / "positive_samples.json").read_text(encoding="utf-8"))}
    reviewed = {row["thread"]: row for row in read_jsonl(args.root / "work" / "references.jsonl")}
    records, _ = read_records(args.root / "context_levels.jsonl")
    records = {r["id"]: r for r in records}
    cells = [c for c in table.discover_cells(args.root / "context_levels.jsonl", None, ["H01_min"])]
    by_pr: dict[str, list] = defaultdict(list)
    for cell in cells:
        by_pr[cell.pr].append(cell)
    counts, rows, builds = Counter(), [], []
    for pr, made in entries.items():
        record = records.get(pr)
        if record is None:
            counts["PRs not built"] += 1
            continue
        seconds = record.get("seconds") or {}
        builds.append({"pr": pr, "status": record["status"], "failed_stage": record.get("failed_stage"),
                       "error": (record.get("error") or "")[:200], "hunks": len(record.get("hunks") or []),
                       "checkout_s": seconds.get("checkout"), "trace_s": seconds.get("trace"), "pack_s": seconds.get("pack"),
                       "warnings": record.get("warnings") or []})
        if record["status"] != "ok":
            counts["PRs failed to build"] += 1
            continue
        counts["PRs built"] += 1
        units = {unit["id"]: unit for unit in hunk_units([record])}
        texts = [ccrb.normalize_text(level["text"]) for unit in units.values() for level in unit["levels"].values() if level.get("text")]
        for ref in made["comments"]:
            counts["references"] += 1
            on = [c for c in by_pr[pr] if table._in_hunk(ref, c)]
            row = {"thread": ref["thread"], "pr": pr, "hunks_holding_it": len(on), "hunk": on[0].entry if on else None}
            counts["reference on no hunk" if not on else "reference on one hunk" if len(on) == 1 else "reference on several hunks"] += 1
            api = reviewed[ref["thread"]]["hunk_as_reviewed"]
            if len(on) == 1:
                level = units[on[0].entry]["levels"]["H01_min"]
                header, body = built_hunk(level["text"], level["sections"])
                seen = ccrb.hunk_body(api)
                # The start lines must agree; the line counts may not, as GitHub and git can end a hunk differently.
                ours, theirs = ccrb._HUNK.match(header), ccrb._HUNK.match(api)
                same_start = bool(ours and theirs) and ours.group(1, 3) == theirs.group(1, 3)
                row["same_hunk_start"] = same_start
                row["same_code_up_to_the_comment"] = same_start and body[:len(seen)] == seen
                row["commented_line_same_text"] = bool(seen) and line_text(header, body, ref["side"], ref["to_line"]) == seen[-1][1:]
                counts["hunk is the code the reviewer saw"] += row["same_code_up_to_the_comment"]
                counts["commented line has the same text"] += row["commented_line_same_text"]
            wanted = ccrb.normalize_text(ref["note"])
            row["comment_text_in_a_level"] = len(wanted) >= 30 and any(wanted in text for text in texts)
            counts["comment text appears in a level"] += row["comment_text_in_a_level"]
            rows.append(row)
    report = {"counts": dict(counts), "builds": builds, "references": rows}
    write_json(args.root / "work" / "check_report.json", report)
    for key, value in counts.items():
        print(f"  {value:4d}  {key}")
    print(f"wrote {args.root / 'work' / 'check_report.json'}")
    return 0


def _repo_number(url: str) -> tuple[str, int]:
    from src.tracer_context.aacr import parse_pr_url
    owner, name, number = parse_pr_url(url)
    return f"{owner}/{name}", number


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, run, text: str):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("root", type=Path, help="the dataset folder, e.g. data/ccrb_hunk")
        sub.set_defaults(run=run)
        return sub

    sub = command("threads", cmd_threads, "one row per comment thread")
    sub.add_argument("--source", type=Path, default=SOURCE, help="folder of ContextCRBench records (<id>.json)")
    sub = command("lookup", cmd_lookup, "look candidate PRs up on GitHub")
    sub.add_argument("--candidates", type=int, default=12, help="candidate PRs per language (default 12)")
    sub.add_argument("--per-repo", type=int, default=2, help="at most this many candidates per repository (default 2)")
    sub.add_argument("--seed", type=int, default=536)
    sub.add_argument("--exclude-dataset", type=Path, default=AACR, help="leave out PRs that are in this AACR-shaped file")
    sub = command("write", cmd_write, "write positive_samples.json")
    sub.add_argument("--per-language", type=int, default=3, help="PRs to select per language (default 3)")
    sub.add_argument("--per-repo", type=int, default=1, help="at most this many selected PRs per repository (default 1)")
    sub.add_argument("--max-files", type=int, default=30, help="leave out PRs that change more files (default 30)")
    sub.add_argument("--max-lines", type=int, default=2000, help="leave out PRs that add and remove more lines (default 2000)")
    sub.add_argument("--fresh", action="store_true",
                     help="select from scratch; by default the PRs the last write selected are kept, first and whatever the quotas")
    command("check", cmd_check, "check the built contexts against what the reviewers saw")
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
