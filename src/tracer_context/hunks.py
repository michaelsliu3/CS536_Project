"""Split a traced PR into diff hunks and repeat its trace from one hunk.

AACR-Bench's paper reviews one diff hunk per call for its non-agent rows. A hunk here is one
`@@` block of the diff the reviewer sees (`git diff -U3`, the trace's `diff`), not one of the
trace's `hunks`, which are runs of changed lines with no surrounding lines.

`HunkTracer(trace).restrict(hunk)` returns a trace of the same shape that starts from the hunk
alone, so `build.build_context` packs the hunk's levels with no change:

  seeds          the changed symbols that overlap the hunk's changed lines (see hunk_seeds)
  callees        what those symbols call, walked again over `call_edges`
  callers        what uses them, walked again over `caller_edges`
  text_hits      the search hits for those symbols' names
  changed_files  the hunk's file
  hunks          the changed lines inside the hunk
  diff           the file's diff header and this hunk
  other_hunks    the PR's other hunks, nearest first (shown by levels with `input.other_hunks`)
  other_changed  the PR's changed lines outside this hunk, by file (levels with
                 `input.mark_changed` say so on the context blocks that contain them)

The trace must have been made with `per_seed_edges=True` (cbm.trace).
"""
from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass

HUNK_VERSION = 2  # how a PR is split and re-walked; part of the hunk dataset's resume key
_HEADER = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass(frozen=True)
class Hunk:
    index: int            # 1-based position in the PR's diff
    file: str             # the new path; the old path for a deleted file
    old_file: str | None  # the old path when it differs (a rename), else None
    deleted_file: bool
    file_header: str      # the file's lines from "diff --git" up to its first hunk
    header: str           # the "@@ -a,b +c,d @@ ..." line
    body: str             # the hunk's lines, header first
    old_start: int
    old_count: int
    new_start: int
    new_count: int

    @property
    def text(self) -> str:
        """The file's diff header and this hunk: a valid diff of the hunk alone."""
        return f"{self.file_header}\n{self.body}" if self.file_header else self.body

    @property
    def new_end(self) -> int:
        return self.new_start + max(self.new_count, 1) - 1

    @property
    def old_end(self) -> int:
        return self.old_start + max(self.old_count, 1) - 1


def hunk_id(entry_id: str, index: int) -> str:
    return f"{entry_id}__h{index:04d}"


def _path(line: str) -> str | None:
    """The path on a `--- ` or `+++ ` line, without git's a/ or b/ prefix; None for /dev/null."""
    path = line[4:].strip().strip('"')
    if path == "/dev/null":
        return None
    return path[2:] if path[:2] in ("a/", "b/") else path


def split_diff(diff_text: str) -> list[Hunk]:
    """Every hunk of a `git diff` text, in order. Files without a hunk (binary files, pure
    renames, mode changes) yield nothing. A hunk's lines are read by the counts in its header,
    so a removed line that itself starts with `-- ` is not taken for a file header."""
    hunks: list[Hunk] = []
    file_header: list[str] = []
    header_open = False  # between a file's "diff --git" line and its first hunk
    old_path = new_path = None
    lines = diff_text.split("\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        match = _HEADER.match(line)
        if match and (old_path or new_path):
            header_open = False
            old_start, new_start = int(match.group(1)), int(match.group(3))
            old_count = int(match.group(2)) if match.group(2) is not None else 1
            new_count = int(match.group(4)) if match.group(4) is not None else 1
            body, old_left, new_left = [line], old_count, new_count
            i += 1
            while i < len(lines) and (old_left > 0 or new_left > 0 or lines[i].startswith("\\")):
                text = lines[i]
                if text.startswith("\\"):      # "\ No newline at end of file"
                    pass
                elif text.startswith("+"):
                    new_left -= 1
                elif text.startswith("-"):
                    old_left -= 1
                else:                          # an unchanged line (a lone "" is an empty one)
                    old_left -= 1
                    new_left -= 1
                body.append(text)
                i += 1
            hunks.append(Hunk(
                index=len(hunks) + 1, file=new_path or old_path,
                old_file=old_path if new_path and old_path and old_path != new_path else None,
                deleted_file=new_path is None, file_header="\n".join(file_header), header=line,
                body="\n".join(body), old_start=old_start, old_count=old_count,
                new_start=new_start, new_count=new_count))
            continue
        if line.startswith("diff --git "):
            file_header, old_path, new_path, header_open = [line], None, None, True
        elif header_open:
            file_header.append(line)
            if line.startswith("--- "):
                old_path = _path(line)
            elif line.startswith("+++ "):
                new_path = _path(line)
        i += 1
    return hunks


def changed_ranges(trace: dict, hunk: Hunk) -> list[list[int]]:
    """The trace's runs of changed lines (new side) that lie inside the hunk."""
    if hunk.deleted_file:
        return []
    return [list(r) for r in trace["hunks"].get(hunk.file, []) if r[1] >= hunk.new_start and r[0] <= hunk.new_end]


def hunk_seeds(trace: dict, hunk: Hunk) -> list[dict]:
    """The changed symbols this hunk changes: those that overlap its changed lines.

    The tool has a second rule, kept here. When no change in a file touches a symbol (only an
    import line changed, say), it takes every symbol of that file as changed and marks it
    `seed_reason: whole_file`. A hunk of such a file gets all of them, as the PR's trace does."""
    ranges = changed_ranges(trace, hunk)
    in_file = [s for s in trace["seeds"] if s["file"] == hunk.file]
    touched = [s for s in in_file if any(int(s["start"]) <= b and int(s["end"]) >= a for a, b in ranges)]
    if touched or not ranges:
        return touched
    return [s for s in in_file if s.get("seed_reason") == "whole_file"]


def other_hunks(hunk: Hunk, hunks: list[Hunk]) -> list[dict]:
    """The PR's other hunks, nearest first: the same file by distance, then the other files in
    diff order."""
    same = sorted((h for h in hunks if h.index != hunk.index and h.file == hunk.file),
                  key=lambda h: (abs(h.index - hunk.index), h.index))
    rest = [h for h in hunks if h.file != hunk.file]
    return [{"index": h.index, "file": h.file, "file_header": h.file_header, "body": h.body,
             "new_start": h.new_start, "new_count": h.new_count, "deleted_file": h.deleted_file,
             "section": h.header.split("@@", 2)[2].strip() if h.header.count("@@") >= 2 else ""}
            for h in same + rest]


def _walk(start: set[str], links: dict[str, list], depth: int) -> dict[str, tuple[int, str, str]]:
    """Breadth-first walk from `start` over `links` ({name: [(next name, link type, rank)]}):
    each reached name's (hop, the name it was reached from, the link type). Among several ways
    to reach a name at the same hop, the first by (rank, name) is kept."""
    reached: dict[str, tuple[int, str, str]] = {}
    frontier = sorted(start)
    for hop in range(1, depth + 1):
        found: dict[str, tuple[int, str, str]] = {}
        for name in frontier:
            for nxt, kind, rank in links.get(name, ()):
                if nxt in start or nxt in reached:
                    continue
                best = found.get(nxt)
                if best is None or (rank, name) < (best[0], best[1]):
                    found[nxt] = (rank, name, kind)
        for nxt, (_, via, kind) in found.items():
            reached[nxt] = (hop, via, kind)
        frontier = sorted(found)
        if not frontier:
            break
    return reached


class HunkTracer:
    """One PR's trace, ready to be repeated from each of its hunks."""

    def __init__(self, trace: dict, hunks: list[Hunk] | None = None):
        if "call_edges" not in trace or "caller_edges" not in trace:
            raise ValueError("this trace has no per-symbol links; trace the PR with per_seed_edges=True")
        self.trace = trace
        self.hunks = split_diff(trace["diff"]) if hunks is None else hunks
        settings = trace["tracer_settings"]
        self.callee_depth, self.caller_depth = settings["callees"]["depth"], settings["callers"]["depth"]
        order = settings["callers"]["edge_types"]
        self.calls, self.used_by = defaultdict(list), defaultdict(list)
        for a, b in trace["call_edges"]:
            self.calls[a].append((b, "CALLS", 0))
        for a, kind, b in trace["caller_edges"]:
            self.used_by[b].append((a, kind, order.index(kind) if kind in order else len(order)))
        # label and file of every symbol a walk can reach: the tool's rows, else a changed symbol
        self.callee_rows = {**{s["qn"]: s for s in trace["seeds"]}, **{r["qn"]: r for r in trace["callees"]}}
        self.caller_rows = {**{s["qn"]: s for s in trace["seeds"]}, **{r["qn"]: r for r in trace["callers"]}}

    @staticmethod
    def _row(rows: dict, name: str) -> dict:
        known = rows.get(name, {})
        return {"qn": name, "label": known.get("label"), "file": known.get("file")}

    def restrict(self, hunk: Hunk) -> dict:
        """The trace as if the PR changed only this hunk (see the module docstring)."""
        trace = self.trace
        seeds = hunk_seeds(trace, hunk)
        start = {s["qn"] for s in seeds}
        reached = _walk(start, self.calls, self.callee_depth)
        callees = sorted(({**self._row(self.callee_rows, name), "hop": hop} for name, (hop, _, _) in reached.items()),
                         key=lambda r: (r["hop"], r["qn"]))
        inside = start | set(reached)
        reached_by = _walk(start, self.used_by, self.caller_depth)
        callers = sorted(({**self._row(self.caller_rows, name), "hop": hop, "via_qn": via, "via_edge": kind}
                          for name, (hop, via, kind) in reached_by.items()), key=lambda r: (r["hop"], r["qn"]))
        ranges = changed_ranges(trace, hunk)
        mine = {tuple(r) for r in ranges}
        elsewhere = {path: [list(r) for r in runs if path != hunk.file or tuple(r) not in mine]
                     for path, runs in trace["hunks"].items()}
        return {
            **trace,
            "seeds": seeds,
            "callees": callees,
            "callee_edges": sorted([a, b] for a, b in trace["call_edges"] if a in inside and b in reached),
            "callers": callers,
            "text_hits": {name: entry for name, entry in trace.get("text_hits", {}).items()
                          if start.intersection(entry.get("seeds", ()))},
            "changed_files": [hunk.file] if hunk.file in trace["changed_files"] else [],
            "hunks": {hunk.file: ranges} if ranges else {},
            "diff": hunk.text,
            "other_hunks": other_hunks(hunk, self.hunks),
            "other_changed": {path: runs for path, runs in elsewhere.items() if runs},
        }

    def check_links(self) -> dict:
        """How well a walk from all changed symbols over the kept links repeats the tool's own
        walk. Counts only; a mismatch means a hunk's callers or callees may differ from what
        the tool would report for that hunk alone."""
        trace = self.trace
        start = {s["qn"] for s in trace["seeds"]}
        out = {}
        for name, rows, links, depth in (("callees", trace["callees"], self.calls, self.callee_depth),
                                         ("callers", trace["callers"], self.used_by, self.caller_depth)):
            tool = {r["qn"]: int(r["hop"]) for r in rows if r["qn"] not in start}
            walked = {qn: hop for qn, (hop, _, _) in _walk(start, links, depth).items()}
            out[name] = {"tool": len(tool), "walked": len(walked),
                         "not_walked": len(set(tool) - set(walked)), "only_walked": len(set(walked) - set(tool)),
                         "hop_differs": sum(1 for qn in set(tool) & set(walked) if tool[qn] != walked[qn])}
        return out


def restrict_trace(trace: dict, hunk: Hunk, hunks: list[Hunk] | None = None) -> dict:
    """`HunkTracer(trace, hunks).restrict(hunk)`, for a single hunk."""
    return HunkTracer(trace, hunks).restrict(hunk)
