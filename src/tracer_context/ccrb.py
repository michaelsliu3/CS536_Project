"""ContextCRBench records as dataset entries in AACR's shape, so the dataset builder can read them.

One ContextCRBench record is one review comment thread on one diff hunk. Three facts about its
fields decide how it is converted (checked on GitHub, November 20, 2026):

- `commit_id` is the pull request's head and `original_commit_id` its base. They are not the
  commits of the comment.
- The stored hunk and file contents are the pull request's final state. The comment was usually
  made on an earlier commit, and the code often changed afterwards. Only GitHub knows that
  commit, so each PR is looked up there (`match_comment`).
- `line` and `original_line` both point into the final file. The commented line at review time is
  the last line of the hunk GitHub returns with the comment (`commented_line`).

Nothing here calls GitHub or reads files: scripts/dataset/contextcrbench.py does both.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter

LANGUAGES = {"c": "C", "cpp": "C++", "c_sharp": "C#", "go": "Go", "java": "Java", "javascript": "JavaScript",
             "python": "Python", "ruby": "Ruby", "rust": "Rust", "typescript": "TypeScript"}

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
_SPACE = re.compile(r"\s+")


def normalize_text(text: str | None) -> str:
    """Text with every run of whitespace as one space: GitHub and the dataset differ in line ends."""
    return _SPACE.sub(" ", text or "").strip()


def pr_id(repo: str, number: int) -> str:
    """The entry id the builder derives from the PR address: owner__repo__number."""
    owner, name = repo.split("/", 1)
    return f"{owner}__{name}__{number}"


def pr_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/pull/{number}"


def _thread_key(record: dict) -> tuple:
    comments = record.get("review_comment") or []
    digest = hashlib.sha1("\x00".join(comments).encode("utf-8", "replace")).hexdigest()
    return record["full_name"], record["pr_number"], record["path"], record["line"], record["side"], digest


def collect_threads(records) -> list[dict]:
    """One row per comment thread, in first-seen order. Records that repeat a thread (the same PR,
    file, line, side and comment texts) are merged; `ccrb_ids` lists them. The stored hunk, the
    stored files and the conversation comments are left behind."""
    threads: dict[tuple, dict] = {}
    for record in records:
        comments = record.get("review_comment") or []
        if not comments or not normalize_text(comments[0]):
            continue
        key = _thread_key(record)
        if key in threads:
            threads[key]["ccrb_ids"].append(record["id"])
            continue
        threads[key] = {
            "thread": f"{pr_id(record['full_name'], record['pr_number'])}::{record['id']}",
            "ccrb_ids": [record["id"]], "repo": record["full_name"], "pr": record["pr_number"],
            "language": LANGUAGES.get(record["lang"], record["lang"]),
            "base_commit": record["original_commit_id"], "head_commit": record["commit_id"],
            "path": record["path"], "side": (record.get("side") or "RIGHT").lower(),
            "final_line": record.get("line"), "comment": comments[0], "replies": list(comments[1:]),
            "commented_at": record.get("created_at"), "merged": bool(record.get("merged")),
            "pr_title": record.get("pr_title") or "",
        }
    return list(threads.values())


def hunk_body(diff_hunk: str) -> list[str]:
    """The lines of a hunk after its header, without a trailing empty line or "\\ No newline" marks."""
    lines = [line.rstrip("\r") for line in (diff_hunk or "").split("\n")[1:]]
    if lines and lines[-1] == "":
        lines.pop()
    return [line for line in lines if not line.startswith("\\")]


def commented_line(diff_hunk: str, side: str) -> int | None:
    """The line number, on `side`, of the last line of a hunk as GitHub returns it with a review
    comment: the hunk is cut at the commented line. None when the hunk cannot be read or shows no
    line on that side."""
    match = _HUNK.match(diff_hunk or "")
    if not match:
        return None
    body = hunk_body(diff_hunk)
    if side == "left":
        start, shown = int(match.group(1)), sum(1 for line in body if not line.startswith("+"))
    else:
        start, shown = int(match.group(3)), sum(1 for line in body if not line.startswith("-"))
    return start + shown - 1 if shown else None


def match_comment(thread: dict, github_comments: list[dict]) -> dict | None:
    """The thread's first comment among a PR's review comments from GitHub: the same file and the
    same text. With several, the one made at the thread's time, then a thread's first comment."""
    wanted = normalize_text(thread["comment"])
    found = [c for c in github_comments if c.get("path") == thread["path"] and normalize_text(c.get("body")) == wanted]
    if not found:
        return None
    found.sort(key=lambda c: (c.get("created_at") != thread.get("commented_at"), c.get("in_reply_to_id") is not None))
    return found[0]


def reference(thread: dict, comment: dict) -> dict | None:
    """The thread as a reference comment in AACR's shape, with its lines at the reviewed commit.
    None when the commented line cannot be worked out from GitHub's hunk."""
    side = (comment.get("side") or "RIGHT").lower()
    last = commented_line(comment.get("diff_hunk") or "", side)
    if last is None:
        return None
    first, start, end = last, comment.get("original_start_line"), comment.get("original_line")
    if start is not None and end is not None and (comment.get("start_side") or comment.get("side")) == comment.get("side"):
        first = max(1, last - max(0, end - start))
    return {"is_ai_comment": False, "note": thread["comment"], "path": thread["path"], "side": side, "source_model": "",
            "from_line": first, "to_line": last, "category": None, "context": None,
            "thread": thread["thread"], "replies": thread["replies"], "ccrb_ids": thread["ccrb_ids"],
            "github_comment_id": comment.get("id"), "commented_at": comment.get("created_at"),
            "commenter_association": comment.get("author_association")}


def choose_commit(matched: list[tuple[dict, dict]]) -> str | None:
    """The commit to rebuild a PR at: the one most of its threads were made on, the earliest
    commented on a tie. `matched` is [(thread, GitHub comment)]."""
    if not matched:
        return None
    counts = Counter(comment["original_commit_id"] for _, comment in matched)
    earliest: dict[str, str] = {}
    for _, comment in matched:
        sha, when = comment["original_commit_id"], comment.get("created_at") or ""
        earliest[sha] = min(earliest.get(sha, when), when)
    return min(counts, key=lambda sha: (-counts[sha], earliest[sha], sha))


def entry(thread: dict, pull: dict, target_commit: str, references: list[dict]) -> dict:
    """One dataset entry in AACR's shape. `change_line_count` is unknown here and written as 0; it
    feeds one audit flag only. `category` is left for the labelling step."""
    return {"change_line_count": 0, "project_main_language": thread["language"],
            "source_commit": thread["base_commit"], "target_commit": target_commit,
            "githubPrUrl": pr_url(thread["repo"], thread["pr"]), "created_at": pull.get("created_at"),
            "merged_at": pull.get("merged_at"), "category": None, "comments": references,
            "head_commit": thread["head_commit"], "pr_author": (pull.get("user") or {}).get("login")}
