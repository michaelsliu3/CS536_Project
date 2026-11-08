"""AACR-Bench entries: stable IDs, checkouts at the reviewed commit, and per-entry checks.

Records use `source_commit` (the PR base) and `target_commit` (the reviewed commit). The PR
diff is `source...target`, i.e. from their merge base to the target. Reference comments are
read only to count whether their line numbers exist; their text never leaves this module.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime
from pathlib import Path

from src.tracer_context.build import parse_hunks

BASE_BRANCH = "aacr-base"  # local branch at source_commit, passed to the tracer as --base
_PR_URL = re.compile(r"^https://github\.com/([^/\s]+)/([^/\s]+)/pull/(\d+)/?$")


class FetchError(RuntimeError):
    pass


def parse_pr_url(url: str) -> tuple[str, str, int]:
    match = _PR_URL.match(url.strip())
    if not match:
        raise ValueError(f"not a GitHub pull request URL: {url!r}")
    return match.group(1), match.group(2), int(match.group(3))


def entry_id(record: dict) -> str:
    owner, repo, number = parse_pr_url(record["githubPrUrl"])
    return f"{owner}__{repo}__{number}"


def file_sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def load_entries(path: Path) -> list[dict]:
    """[{index, id, record}] in file order; IDs must be unique."""
    records = json.loads(Path(path).read_text(encoding="utf-8"))
    entries = [{"index": i, "id": entry_id(r), "record": r} for i, r in enumerate(records)]
    ids = [e["id"] for e in entries]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"duplicate PRs in {path}: {duplicates[:5]}")
    return entries


def select_entries(entries, ids=None, languages=None, limit=None):
    """Filter by entry IDs or file indices, and by main language; keep file order."""
    chosen = entries
    if ids:
        wanted = {str(i) for i in ids}
        chosen = [e for e in chosen if e["id"] in wanted or str(e["index"]) in wanted]
        found = {e["id"] for e in chosen} | {str(e["index"]) for e in chosen}
        missing = sorted(wanted - found)
        if missing:
            raise ValueError(f"unknown entries: {missing}")
    if languages:
        wanted_languages = {lang.casefold() for lang in languages}
        chosen = [e for e in chosen if e["record"]["project_main_language"].casefold() in wanted_languages]
    return chosen[:limit] if limit is not None else chosen


def _git(repo: Path, *args: str, check: bool = True, timeout: int = 3600) -> subprocess.CompletedProcess:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=timeout)
    if check and result.returncode != 0:
        raise FetchError(f"git {' '.join(args)} failed: {result.stderr.strip()[-600:]}")
    return result


def _has_commit(repo: Path, sha: str) -> bool:
    return _git(repo, "cat-file", "-e", f"{sha}^{{commit}}", check=False).returncode == 0


def _merge_base(repo: Path, a: str, b: str) -> str | None:
    result = _git(repo, "merge-base", a, b, check=False)
    return result.stdout.strip() or None


def prepare_checkout(repos_dir: Path, record: dict, depth: int = 50, max_depth: int = 3200,
                     remote_url: str | None = None) -> dict:
    """Check out `target_commit` (detached, clean) in a per-repository clone under `repos_dir`,
    with branch `aacr-base` at `source_commit` and enough history for their merge base."""
    owner, name, number = parse_pr_url(record["githubPrUrl"])
    source, target = record["source_commit"], record["target_commit"]
    repo = Path(repos_dir) / f"{owner}__{name}"
    log = []
    if not (repo / ".git").exists():
        repo.mkdir(parents=True, exist_ok=True)
        _git(repo, "init", "-q")
        _git(repo, "remote", "add", "origin", remote_url or f"https://github.com/{owner}/{name}.git")
        log.append("init")

    missing = [sha for sha in (source, target) if not _has_commit(repo, sha)]
    if missing:
        if _git(repo, "fetch", "-q", f"--depth={depth}", "origin", *missing, check=False).returncode != 0:
            for sha in missing:  # fetch separately to find which one is unavailable
                _git(repo, "fetch", "-q", f"--depth={depth}", "origin", sha, check=False)
        log.append(f"fetch depth {depth}")
    if not _has_commit(repo, target):
        # A force-pushed PR can leave the reviewed commit reachable only from the PR ref.
        pr_ref = f"refs/remotes/origin/pr/{number}"
        fetch_depth = depth
        while not _has_commit(repo, target) and fetch_depth <= max_depth:
            if _git(repo, "fetch", "-q", f"--depth={fetch_depth}", "origin",
                    f"+refs/pull/{number}/head:{pr_ref}", check=False).returncode != 0:
                break
            log.append(f"fetch pull/{number}/head depth {fetch_depth}")
            fetch_depth *= 4
    for label, sha in (("source_commit", source), ("target_commit", target)):
        if not _has_commit(repo, sha):
            raise FetchError(f"{label} {sha} is not available from {owner}/{name}")

    merge_base, step = _merge_base(repo, source, target), depth
    while merge_base is None:
        if step > max_depth:
            raise FetchError(f"no merge base within about {max_depth} commits of history")
        _git(repo, "fetch", "-q", f"--deepen={step}", "origin", source, target)
        log.append(f"deepen {step}")
        merge_base, step = _merge_base(repo, source, target), step * 2

    _git(repo, "branch", "-f", BASE_BRANCH, source)
    _git(repo, "checkout", "-q", "-f", "--detach", target)
    _git(repo, "clean", "-q", "-ffdx")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    if head != target or _git(repo, "status", "--porcelain").stdout.strip():
        raise FetchError(f"checkout of {target} is not clean")
    return {"repo": str(repo), "base": BASE_BRANCH, "merge_base": merge_base, "fetch_log": log}


_PR_QUERY = ("query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name)"
             "{pullRequest(number:$number){title body createdAt lastEditedAt}}}")


def fetch_pr_text(url: str) -> dict:
    """Current PR title and description from GitHub (needs an authenticated `gh`).

    GitHub returns the latest description; `last_edited_at` says whether it was edited at all.
    Title edits are not tracked by this field."""
    owner, name, number = parse_pr_url(url)
    result = subprocess.run(["gh", "api", "graphql", "-f", f"query={_PR_QUERY}", "-f", f"owner={owner}",
                             "-f", f"name={name}", "-F", f"number={number}"],
                            capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise FetchError(f"gh api graphql failed: {result.stderr.strip()[:500]}")
    pr = (json.loads(result.stdout).get("data") or {}).get("repository", {}).get("pullRequest")
    if not pr:
        raise FetchError(f"pull request not found: {url}")
    return {"url": url, "title": pr["title"], "body": pr["body"] or "", "created_at": pr["createdAt"],
            "last_edited_at": pr["lastEditedAt"]}


def commit_time(repo: Path, sha: str) -> str:
    return _git(repo, "show", "-s", "--format=%cI", sha).stdout.strip()


def edited_after(last_edited_at: str | None, when: str) -> bool:
    """True when the PR description was last edited after `when` (an ISO time)."""
    if not last_edited_at:
        return False
    parse = lambda value: datetime.fromisoformat(value.replace("Z", "+00:00"))  # noqa: E731
    return parse(last_edited_at) > parse(when)


def entry_checks(repo: Path, record: dict, merge_base: str) -> dict:
    """Automatic per-entry checks (the dataset audit, as counts)."""
    target = record["target_commit"]
    files, lines = 0, 0
    for row in _git(repo, "diff", "--numstat", "--no-renames", merge_base, target).stdout.splitlines():
        added, deleted, _ = row.split("\t", 2)
        files += 1
        lines += (int(added) if added.isdigit() else 0) + (int(deleted) if deleted.isdigit() else 0)

    lengths: dict[tuple[str, str], int | None] = {}

    def length(rev: str, path: str) -> int | None:
        if (rev, path) not in lengths:
            result = _git(repo, "show", f"{rev}:{path}", check=False)
            lengths[rev, path] = len(result.stdout.splitlines()) if result.returncode == 0 else None
        return lengths[rev, path]

    # Review comments can only sit on lines the PR diff shows: changed lines plus 3 lines of context.
    shown = parse_hunks(_git(repo, "diff", "-U3", "--no-color", "--no-ext-diff", merge_base, target).stdout)
    comments = {"ok": 0, "missing_file": 0, "out_of_range": 0, "right_side_outside_diff": 0}
    for comment in record.get("comments", []):
        rev = merge_base if comment.get("side") == "left" else target
        size = length(rev, comment["path"])
        first, last = int(comment["from_line"]), int(comment["to_line"])
        if size is None:
            comments["missing_file"] += 1
        elif not 1 <= first <= last <= size:
            comments["out_of_range"] += 1
        else:
            comments["ok"] += 1
            if comment.get("side") != "left" and not any(
                    a <= last and first <= b for a, b in shown.get(comment["path"], [])):
                comments["right_side_outside_diff"] += 1
    return {"diff_files": files, "diff_lines": lines, "change_line_count": record["change_line_count"],
            "diff_lines_match": lines == record["change_line_count"], "comment_lines": comments}
