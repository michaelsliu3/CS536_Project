"""Turn a codebase-memory-mcp trace into a ranked, token-budgeted review input.

Pure functions over the trace dict written by ``src.tracer_context.cbm.trace`` and the
files of the checked-out PR head. No model or tool calls happen here; token counting
is supplied by the caller.

The output has up to three sections: the PR title and description, the diff, and the
repository context (code items ranked and packed into the token budget). A config can
name a parent level; the parent's chosen items are then included first, so every line a
smaller level shows is also shown by the larger one.
"""
from __future__ import annotations

import ast
import copy
import hashlib
import json
import re
import warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable

KINDS = ("changed", "file", "callee", "caller", "test", "search", "guide", "doc")
RANK_KEYS = ("hop", "kind", "edge", "same_file", "size")
OVERSIZE_POLICIES = ("hunks", "head", "skip")
RENDER_VERSION = 2
SEPARATOR = "\n\n"  # between items in the context text
CONTEXT_HEADING = "## Repository context"
WHOLE_FILE = 10 ** 9  # end line meaning "to the end of the file"
_NOT_CODE_LABELS = ("Section", "File", "Guide", "TextHit")

# Expected config shape: a type, a tuple of allowed values, or a nested dict.
_OPT_INT = (int, type(None))
_OPT_STR = (str, type(None))
SCHEMA = {
    "version": int,
    "level": {"name": str, "parent": _OPT_STR},
    "input": {"pr_text": bool, "diff": bool, "strip_html_comments": bool},
    "tracer": {
        "binary": str,
        "workers": int,
        "index_mode": ("full", "moderate", "fast"),
        "callers": {"depth": int, "edge_types": list},
        "callees": {"depth": int},
    },
    "selection": {
        "seed_scope": ("innermost", "all"),
        "member_labels": list,
        "fold_max_lines": int,
        "drop_labels": list,
        "drop_builtins": bool,
        "test_globs": list,
        "file_exclude_globs": list,
        "guide_scope": ("root", "ancestors"),
        "search": {"min_name_length": int, "max_hits_per_name": int, "window_lines": int},
        "limits": dict,
    },
    "ranking": {"rank_by": list, "kind_order": list, "edge_order": list},
    "packing": {
        "token_budget": int,
        "tokenizer": str,
        "kind_shares": dict,
        "redistribute_unused": bool,
        "max_lines_per_item": dict,
        "oversize": dict,
        "hunk_context_lines": dict,
        "include_decorators": bool,
        "extend_doc_sections": bool,
        "line_numbers": bool,
    },
}

LANG_BY_EXT = {
    ".py": "python", ".java": "java", ".kt": "kotlin", ".go": "go", ".rs": "rust",
    ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "jsx",
    ".c": "c", ".h": "c", ".cc": "cpp", ".cpp": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".cs": "csharp", ".rb": "ruby", ".php": "php", ".swift": "swift", ".scala": "scala",
    ".md": "markdown", ".rst": "rst", ".sh": "bash", ".yaml": "yaml", ".yml": "yaml",
}
# Lines directly above a definition that belong to it (decorators, annotations, attributes).
_ANNOTATION = {
    ".py": r"\s*@", ".java": r"\s*@\w", ".kt": r"\s*@\w", ".scala": r"\s*@\w",
    ".ts": r"\s*@\w", ".tsx": r"\s*@\w", ".js": r"\s*@\w", ".jsx": r"\s*@\w",
    ".swift": r"\s*@\w", ".dart": r"\s*@\w", ".rs": r"\s*#\[", ".php": r"\s*#\[",
    ".cs": r"\s*\[[A-Za-z_]",
}
_HEADING = re.compile(r"^(#{1,6})\s")


def _check(value, spec, where):
    if isinstance(spec, dict):
        if not isinstance(value, dict):
            raise ValueError(f"{where}: expected a mapping")
        missing, unknown = set(spec) - set(value), set(value) - set(spec)
        if missing or unknown:
            raise ValueError(f"{where}: missing keys {sorted(missing)}, unknown keys {sorted(unknown)}")
        for key, sub in spec.items():
            _check(value[key], sub, f"{where}.{key}")
    elif isinstance(spec, tuple) and all(isinstance(s, str) for s in spec):
        if value not in spec:
            raise ValueError(f"{where}: {value!r} is not one of {list(spec)}")
    else:
        types = spec if isinstance(spec, tuple) else (spec,)
        if isinstance(value, bool) and bool not in types:
            raise ValueError(f"{where}: expected {spec}, got a boolean")
        if not isinstance(value, types):
            raise ValueError(f"{where}: expected {spec}, got {type(value).__name__}")


def _per_kind(packing, key, check, message):
    table = packing[key]
    if "default" not in table or not set(table) <= set(KINDS) | {"default"}:
        raise ValueError(f"config.packing.{key} needs 'default' and only kind names as keys")
    if not all(check(v) for v in table.values()):
        raise ValueError(f"config.packing.{key} values must be {message}")


def validate_config(cfg: dict) -> dict:
    """Reject missing or unknown keys and bad values, so typos never pass silently."""
    _check(cfg, SCHEMA, "config")
    if cfg["version"] != 1:
        raise ValueError("config.version must be 1")
    level = cfg["level"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", level["name"]) or level["parent"] == level["name"]:
        raise ValueError("config.level.name must be a simple name, and not its own parent")
    if cfg["tracer"]["workers"] < 1:
        raise ValueError("config.tracer.workers must be >= 1")
    for walk in ("callers", "callees"):
        if not 1 <= cfg["tracer"][walk]["depth"] <= 15:
            raise ValueError(f"config.tracer.{walk}.depth must be 1-15")
    selection = cfg["selection"]
    limits = selection["limits"]
    if set(limits) != set(KINDS):
        raise ValueError(f"config.selection.limits must have exactly {list(KINDS)}")
    for kind, limit in limits.items():
        _check(limit, {"max_hop": _OPT_INT, "max_items": _OPT_INT}, f"config.selection.limits.{kind}")
        for key, value in limit.items():
            if value is not None and value < 0:
                raise ValueError(f"config.selection.limits.{kind}.{key} must be >= 0 or null")
    if selection["fold_max_lines"] < 0:
        raise ValueError("config.selection.fold_max_lines must be >= 0")
    search = selection["search"]
    if search["min_name_length"] < 1 or search["max_hits_per_name"] < 0 or search["window_lines"] < 0:
        raise ValueError("config.selection.search: min_name_length >= 1, others >= 0")
    ranking = cfg["ranking"]
    if not set(ranking["rank_by"]) <= set(RANK_KEYS) or len(set(ranking["rank_by"])) != len(ranking["rank_by"]):
        raise ValueError(f"config.ranking.rank_by: use distinct keys from {list(RANK_KEYS)}")
    if sorted(ranking["kind_order"]) != sorted(KINDS):
        raise ValueError(f"config.ranking.kind_order must list each of {list(KINDS)} once")
    packing = cfg["packing"]
    is_int = lambda v, low: isinstance(v, int) and not isinstance(v, bool) and v >= low  # noqa: E731
    _per_kind(packing, "oversize", lambda v: v in OVERSIZE_POLICIES, f"one of {list(OVERSIZE_POLICIES)}")
    _per_kind(packing, "max_lines_per_item", lambda v: is_int(v, 1), "integers >= 1")
    _per_kind(packing, "hunk_context_lines", lambda v: is_int(v, 0), "integers >= 0")
    shares = packing["kind_shares"]
    if not set(shares) <= set(KINDS):
        raise ValueError(f"config.packing.kind_shares keys must be in {list(KINDS)}")
    for kind, share in shares.items():
        if share is not None and (isinstance(share, bool) or not isinstance(share, (int, float))
                                  or not 0 <= share <= 1):
            raise ValueError(f"config.packing.kind_shares.{kind} must be between 0 and 1, or null")
    if sum(s for s in shares.values() if s is not None) > 1 + 1e-9:
        raise ValueError("config.packing.kind_shares must add up to at most 1")
    if packing["token_budget"] < 0:
        raise ValueError("config.packing.token_budget must be >= 0")
    return cfg


def merge_config(base: dict, override: dict) -> dict:
    """Deep merge for config inheritance: mappings merge key by key, anything else replaces."""
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge_config(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def config_sha256(cfg: dict) -> str:
    return hashlib.sha256(json.dumps(cfg, sort_keys=True).encode("utf-8")).hexdigest()


def glob_to_regex(pattern: str) -> re.Pattern:
    """`**/` = any directories (or none), `**` = anything, `*`/`?` stay inside one path part."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def is_test_path(path: str, globs) -> bool:
    """True when `path` matches any of the globs (used for test paths and file exclusions)."""
    return any(glob_to_regex(g).match(path) for g in globs)


def parse_hunks(diff_text: str) -> dict[str, list[list[int]]]:
    """New-side line ranges per file from `git diff`. With -U0 these are the changed lines;
    pure deletions become a one-line marker at the following line."""
    hunks: dict[str, list[list[int]]] = {}
    current = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip().strip('"')
            current = None if path == "/dev/null" else path[2:] if path.startswith("b/") else path
        elif line.startswith("@@") and current is not None:
            match = re.match(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
            if match:
                start, count = int(match.group(1)), int(match.group(2) or 1)
                hunks.setdefault(current, []).append(
                    [max(start, 1), max(start, 1)] if count == 0 else [start, start + count - 1])
    return hunks


@dataclass(frozen=True)
class Item:
    qn: str
    label: str
    file: str
    start: int
    end: int
    kind: str
    hop: int
    edge: str | None = None
    via: str | None = None
    relations: tuple[str, ...] = ()


def _excluded(item_or_row, reason, **extra):
    get = item_or_row.get if isinstance(item_or_row, dict) else lambda k, d=None: getattr(item_or_row, k, d)
    entry = {"qn": get("qn"), "kind": get("kind"), "hop": get("hop"), "file": get("file"), "reason": reason}
    entry.update(extra)
    return entry


def _guide_items(trace: dict, selection: dict) -> list[Item]:
    """Repository guide files at the root (and .github/.cursor), plus, with `ancestors`,
    guides in any directory that contains a changed file."""
    ancestors = {"."}
    for path in trace["changed_files"]:
        ancestors.update(str(parent) for parent in PurePosixPath(path).parents)
    items = []
    for path in trace.get("guide_files", []):
        parent = str(PurePosixPath(path).parent)
        if parent in (".", ".github", ".cursor/rules") or (
                selection["guide_scope"] == "ancestors" and parent in ancestors):
            items.append(Item(path, "Guide", path, 1, WHOLE_FILE, "guide", 1, "GUIDE"))
    return items


def _search_items(trace: dict, selection: dict) -> tuple[list[Item], list[dict]]:
    """Windows around text-search hits for changed symbols' names, merged per file."""
    search, excluded, windows = selection["search"], [], {}
    for name, entry in sorted(trace.get("text_hits", {}).items()):
        row = {"qn": name, "kind": "search", "hop": 1, "file": None}
        if len(name) < search["min_name_length"]:
            excluded.append(_excluded(row, "search_name_too_short"))
            continue
        if entry["total"] > search["max_hits_per_name"]:
            excluded.append(_excluded(row, "search_name_too_common", hits=entry["total"]))
            continue
        for path, line in entry["hits"]:
            low, high = max(1, line - search["window_lines"]), line + search["window_lines"]
            windows.setdefault(path, []).append((low, high, name))
    items = []
    for path in sorted(windows):
        merged = []
        for low, high, name in sorted(windows[path]):
            if merged and low <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], high)
                merged[-1][2].add(name)
            else:
                merged.append([low, high, {name}])
        for low, high, names in merged:
            items.append(Item(f"{path}:{low}-{high}", "TextHit", path, low, high, "search", 1, "TEXT",
                              ", ".join(sorted(names))))
    return items, excluded


def collect_items(trace: dict, selection: dict) -> tuple[list[Item], list[dict]]:
    """Every candidate item, typed and deduplicated, plus the reasons for dropping others."""
    nodes, excluded = trace["nodes"], []
    seeds = [Item(s["qn"], s["label"], s["file"], int(s["start"]), int(s["end"]), "changed", 0)
             for s in trace["seeds"]]
    members = set(selection["member_labels"])

    def foldable(item):  # struct fields, constants, 1-2 line interface methods, ...
        return item.label in members or item.end - item.start + 1 <= selection["fold_max_lines"]

    def contains(outer, inner):
        return (outer is not inner and outer.file == inner.file and outer.start <= inner.start
                and inner.end <= outer.end and (outer.start, outer.end) != (inner.start, inner.end))

    if selection["seed_scope"] == "innermost":
        # Keep the innermost changed symbols, ignoring members so a struct beats its fields.
        kept = []
        for seed in seeds:
            inner = sorted(t.qn for t in seeds if not foldable(t) and contains(seed, t))
            if inner:
                excluded.append(_excluded(seed, "contains_changed", by=inner[0]))
            else:
                kept.append(seed)
        # A member inside a kept changed symbol is shown as part of it.
        seeds = []
        for seed in kept:
            outer = sorted(c.qn for c in kept if contains(c, seed)) if foldable(seed) else []
            if outer:
                excluded.append(_excluded(seed, "folded_into", by=outer[0]))
            else:
                seeds.append(seed)
    seed_qns = {s["qn"] for s in trace["seeds"]}
    drop_labels = set(selection["drop_labels"]) | members
    candidates = list(seeds)

    for row in trace["callers"]:
        row = {**row, "kind": "caller"}
        node = nodes.get(row["qn"])
        if row["qn"] in seed_qns:
            continue
        if row["label"] in drop_labels:
            excluded.append(_excluded(row, f"label:{row['label']}"))
            continue
        if not node or not node.get("start"):
            excluded.append(_excluded(row, "no_range"))
            continue
        if row["label"] == "Section":
            kind = "doc"
        elif row.get("via_edge") == "TESTS" or is_test_path(node["file"], selection["test_globs"]):
            kind = "test"
        else:
            kind = "caller"
        candidates.append(Item(row["qn"], row["label"], node["file"], node["start"], node["end"],
                               kind, int(row["hop"]), row.get("via_edge"), row.get("via_qn")))

    callee_via = _callee_sources(trace)
    for row in trace["callees"]:
        row = {**row, "kind": "callee"}
        node = nodes.get(row["qn"])
        if row["qn"] in seed_qns:
            continue
        if row["qn"].startswith("builtins.") or (not node and not row.get("file")):
            if selection["drop_builtins"]:
                excluded.append(_excluded(row, "builtin"))
                continue
        if row["label"] in drop_labels:
            excluded.append(_excluded(row, f"label:{row['label']}"))
            continue
        if not node or not node.get("start"):
            excluded.append(_excluded(row, "no_range"))
            continue
        candidates.append(Item(row["qn"], row["label"], node["file"], node["start"], node["end"],
                               "callee", int(row["hop"]), "CALLS", callee_via.get(row["qn"])))

    for path in trace["changed_files"]:  # whole changed files (post-change version)
        if is_test_path(path, selection["file_exclude_globs"]):
            excluded.append(_excluded({"qn": path, "kind": "file", "hop": 0, "file": path}, "file_excluded"))
        else:
            candidates.append(Item(path, "File", path, 1, WHOLE_FILE, "file", 0))
    candidates += _guide_items(trace, selection)
    search_items, search_excluded = _search_items(trace, selection)
    candidates += search_items
    excluded += search_excluded

    # One item per name: keep the closest relation, remember every kind it had.
    by_qn: dict[str, list[Item]] = {}
    for item in candidates:
        by_qn.setdefault(item.qn, []).append(item)
    items = []
    for qn in sorted(by_qn):
        group = sorted(by_qn[qn], key=lambda it: (it.hop, KINDS.index(it.kind), it.edge or "", it.via or ""))
        best = group[0]
        relations = tuple(sorted({it.kind for it in group}, key=KINDS.index))
        items.append(Item(**{**best.__dict__, "relations": relations}))
    return items, excluded


def _callee_sources(trace: dict) -> dict[str, str]:
    """For each callee, the alphabetically first caller one hop closer to the change."""
    hop = {s["qn"]: 0 for s in trace["seeds"]}
    hop.update({row["qn"]: int(row["hop"]) for row in trace["callees"]})
    sources: dict[str, list[str]] = {}
    for a, b in trace.get("callee_edges", []):
        if b in hop and a in hop and hop[a] == hop[b] - 1:
            sources.setdefault(b, []).append(a)
    return {b: sorted(found)[0] for b, found in sources.items()}


def rank_key(item: Item, ranking: dict, changed_files) -> tuple:
    parts = []
    for key in ranking["rank_by"]:
        if key == "hop":
            parts.append(item.hop)
        elif key == "kind":
            parts.append(ranking["kind_order"].index(item.kind))
        elif key == "edge":
            order = ranking["edge_order"]
            parts.append(-1 if item.kind in ("changed", "file") else
                         order.index(item.edge) if item.edge in order else len(order))
        elif key == "same_file":
            parts.append(0 if item.file in changed_files else 1)
        elif key == "size":
            parts.append(item.end - item.start + 1)
    return (*parts, item.file, item.start, item.qn)


def apply_limits(items, limits, ranking, changed_files):
    kept, excluded = [], []
    for kind in KINDS:
        group = sorted((it for it in items if it.kind == kind),
                       key=lambda it: rank_key(it, ranking, changed_files))
        max_hop, max_items = limits[kind]["max_hop"], limits[kind]["max_items"]
        count = 0
        for item in group:
            if max_hop is not None and item.hop > max_hop:
                excluded.append(_excluded(item, "max_hop"))
            elif max_items is not None and count >= max_items:
                excluded.append(_excluded(item, "max_items"))
            else:
                kept.append(item)
                count += 1
    return sorted(kept, key=lambda it: rank_key(it, ranking, changed_files)), excluded


def python_decorator_starts(source: str) -> dict[int, int]:
    """Map each decorated def/class line to its first decorator line."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return {}
    starts = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.decorator_list:
            starts[node.lineno] = min(d.lineno for d in node.decorator_list)
    return starts


def annotation_start(lines: list[str], start: int, path: str, py_starts: dict[int, int] | None = None) -> int:
    """Move a 1-based start line up over decorators/annotations directly above it."""
    suffix = Path(path).suffix.lower()
    if suffix == ".py" and py_starts and start in py_starts:
        return min(start, py_starts[start])
    pattern = _ANNOTATION.get(suffix)
    if not pattern:
        return start
    regex = re.compile(pattern)
    while start > 1 and regex.match(lines[start - 2]):
        start -= 1
    return start


def markdown_section_end(lines: list[str], start: int) -> int | None:
    """Last line of the Markdown section whose heading is on `start` (1-based)."""
    match = _HEADING.match(lines[start - 1]) if 0 < start <= len(lines) else None
    if not match:
        return None
    level, in_fence, end = len(match.group(1)), False, len(lines)
    for index in range(start, len(lines)):
        text = lines[index]
        if text.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            continue
        heading = None if in_fence else _HEADING.match(text)
        if heading and len(heading.group(1)) <= level:
            end = index
            break
    while end > start and not lines[end - 1].strip():
        end -= 1
    return end


def _merge(ranges):
    merged = []
    for a, b in sorted(ranges):
        if merged and a <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [tuple(r) for r in merged]


def _per_kind_value(packing, key, kind):
    return packing[key].get(kind, packing[key]["default"])


def choose_segments(start, end, kind, file_hunks, packing):
    """Line segments to show for an item, or None when an oversize item is skipped."""
    limit = _per_kind_value(packing, "max_lines_per_item", kind)
    if end - start + 1 <= limit:
        return [(start, end)]
    policy = _per_kind_value(packing, "oversize", kind)
    if policy == "skip":
        return None
    if policy == "hunks":
        ctx = _per_kind_value(packing, "hunk_context_lines", kind)
        windows = [(max(start, a - ctx), min(end, b + ctx)) for a, b in file_hunks if b >= start and a <= end]
        if windows:
            segments, shown = [], 0
            for a, b in _merge([(start, min(end, start + ctx))] + windows):
                take = min(b - a + 1, limit - shown)
                if take <= 0:
                    break
                segments.append((a, a + take - 1))
                shown += take
            return segments
    return [(start, start + limit - 1)]


def _header(item: Item, start: int, end: int) -> str:
    if item.kind == "file":
        return f"### changed file: {item.file}  [lines {start}-{end}]"
    if item.kind == "guide":
        return f"### guide: {item.file}  [lines {start}-{end}]"
    if item.kind == "search":
        return f"### text search for {item.via}: [{item.file}:{start}-{end}]"
    title = item.kind if item.kind == "changed" else f"{item.kind} (hop {item.hop})"
    header = f"### {title}: {item.qn}  [{item.file}:{start}-{end}]"
    if item.via and item.kind == "callee":
        header += f"  called by {item.via}"
    elif item.via:
        header += f"  {item.edge or 'LINKS'} {item.via}"
    return header


def _fence(text: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def render_item(item: Item, lines: list[str], segments, start: int, end: int, line_numbers: bool) -> str:
    body, previous = [], None
    for a, b in segments:
        if previous is not None and a > previous + 1:
            body.append(f"... (lines {previous + 1}-{a - 1} omitted)")
        for number in range(a, b + 1):
            text = lines[number - 1]
            body.append(f"{number:>5}| {text}" if line_numbers else text)
        previous = b
    if previous is not None and previous < end:
        body.append(f"... (lines {previous + 1}-{end} omitted)")
    code = "\n".join(body)
    fence = _fence(code)
    language = LANG_BY_EXT.get(Path(item.file).suffix.lower(), "")
    return f"{_header(item, start, end)}\n{fence}{language}\n{code}\n{fence}"


def render_pr(pr: dict, strip_html_comments: bool) -> str:
    body = (pr.get("body") or "").replace("\r\n", "\n")
    if strip_html_comments:  # invisible on GitHub; PR templates are full of them
        body = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    body = re.sub(r"\n{3,}", "\n\n", body).strip()
    return f"## Pull request\n\nTitle: {(pr.get('title') or '').strip()}\n\n{body or '(no description)'}"


def render_diff(diff: str) -> str:
    fence = _fence(diff)
    return f"## Diff\n\n{fence}diff\n{diff.rstrip()}\n{fence}"


def _overlaps(segments_a, segments_b) -> bool:
    return any(a <= d and c <= b for a, b in segments_a for c, d in segments_b)


def build_context(trace: dict, repo: Path, cfg: dict, count_tokens: Callable[[str], int], tokenizer_name: str,
                  pr: dict | None = None, parent_report: dict | None = None) -> tuple[str, dict]:
    """Return (review input text, selection report) for one traced PR.

    `pr` ({title, body, ...}) is required when `input.pr_text` is on. `parent_report` is the
    selection report of this config's parent level; its items are included first."""
    selection, ranking, packing, inputs = cfg["selection"], cfg["ranking"], cfg["packing"], cfg["input"]
    if inputs["pr_text"] and pr is None:
        raise ValueError("this config includes the PR title and description, but no PR text was given")
    if inputs["diff"] and "diff" not in trace:
        raise ValueError("this trace has no diff (an older trace format); trace the PR again")
    if bool(cfg["level"]["parent"]) != (parent_report is not None):
        raise ValueError(f"level {cfg['level']['name']!r}: parent {cfg['level']['parent']!r} needs its report "
                         "(and only then)")
    changed_files = set(trace["changed_files"])
    hunks = trace["hunks"]
    items, excluded = collect_items(trace, selection)
    forced = list(parent_report["included"]) if parent_report else []
    forced_qns = {f["qn"] for f in forced}
    ranked, limited = apply_limits([it for it in items if it.qn not in forced_qns],
                                   selection["limits"], ranking, changed_files)
    excluded += limited

    repo_root = repo.resolve()
    file_cache: dict[str, tuple[list[str] | None, str | None]] = {}
    decorator_cache: dict[str, dict[int, int]] = {}

    def read(path):
        if path not in file_cache:
            full = (repo / path).resolve()
            if not (full.is_file() and repo_root in full.parents):
                file_cache[path] = (None, "missing_file")
            else:
                raw = full.read_bytes()
                file_cache[path] = ((None, "binary") if b"\0" in raw[:8192]
                                    else (raw.decode("utf-8", errors="replace").splitlines(), None))
        return file_cache[path]

    def render(item, lines, segments, start, end):
        block = render_item(item, lines, segments, start, end, packing["line_numbers"])
        # Count the block with the separator that follows it, so the sum tracks the joined text.
        return block, count_tokens(block + SEPARATOR)

    def prepare(item):
        """A rendered candidate, or None after recording why it cannot be shown."""
        lines, problem = read(item.file)
        if lines is None:
            excluded.append(_excluded(item, problem))
            return None
        start, end = max(1, item.start), min(item.end, len(lines))
        if end < start:
            excluded.append(_excluded(item, "bad_range"))
            return None
        if packing["include_decorators"] and item.label not in _NOT_CODE_LABELS:
            if item.file.endswith(".py") and item.file not in decorator_cache:
                decorator_cache[item.file] = python_decorator_starts("\n".join(lines))
            start = annotation_start(lines, start, item.file, decorator_cache.get(item.file))
        if packing["extend_doc_sections"] and item.label == "Section":
            end = max(end, markdown_section_end(lines, start) or end)
        segments = choose_segments(start, end, item.kind, hunks.get(item.file, []), packing)
        if segments is None:
            excluded.append(_excluded(item, "oversize"))
            return None
        block, tokens = render(item, lines, segments, start, end)
        return {"item": item, "lines": lines, "start": start, "end": end, "segments": segments,
                "block": block, "tokens": tokens}

    budget = packing["token_budget"]
    shares = packing["kind_shares"]
    caps = {kind: None if shares.get(kind) is None else int(budget * shares[kind]) for kind in KINDS}
    chosen = []  # in the order items were added
    used, used_by_kind = 0, Counter()

    for entry in forced:  # the parent level's items, exactly as the parent showed them
        item = Item(entry["qn"], entry["label"], entry["file"], entry["start"], entry["end"], entry["kind"],
                    entry["hop"], entry.get("edge"), entry.get("via"), tuple(entry.get("relations", ())))
        lines, problem = read(item.file)
        if lines is None:
            raise ValueError(f"parent item {item.qn} cannot be shown: {problem}")
        segments = [tuple(s) for s in entry["segments"]]
        block, tokens = render(item, lines, segments, entry["start"], entry["end"])
        chosen.append({"item": item, "lines": lines, "start": entry["start"], "end": entry["end"],
                       "segments": segments, "block": block, "tokens": tokens, "pass": 0, "forced": True})
        used += tokens
        used_by_kind[item.kind] += tokens
    if used > budget:
        raise ValueError(f"level {cfg['level']['name']!r}: token_budget {budget} is below the {used} tokens "
                         f"of its parent's items")

    def try_add(prep, pass_number, enforce_caps):
        """Add a candidate if it fits; return (reason, by) when it does not."""
        nonlocal used, chosen
        item = prep["item"]
        blocking = [p for p in chosen if p["item"].file == item.file and _overlaps(p["segments"], prep["segments"])]
        if blocking and item.kind != "file":
            return "overlaps_included", blocking[0]["item"].qn
        if blocking:  # a changed file absorbs the items it overlaps, so their lines stay visible
            merged = _merge(list(prep["segments"]) + [s for p in blocking for s in p["segments"]])
            block, tokens = render(item, prep["lines"], merged, prep["start"], prep["end"])
            prep = {**prep, "segments": merged, "block": block, "tokens": tokens}
        freed = Counter()
        for p in blocking:
            freed[p["item"].kind] += p["tokens"]
        cap = caps[item.kind]
        if enforce_caps and cap is not None and used_by_kind[item.kind] - freed[item.kind] + prep["tokens"] > cap:
            return "kind_share", None
        if used - sum(freed.values()) + prep["tokens"] > budget:
            return "budget", None
        for p in blocking:
            excluded.append(_excluded(p["item"], "superseded_by", by=item.qn, forced=p["forced"]))
        chosen = [c for c in chosen if all(c is not p for p in blocking)]
        used += prep["tokens"] - sum(freed.values())
        used_by_kind.subtract(freed)
        used_by_kind[item.kind] += prep["tokens"]
        chosen.append({**prep, "pass": pass_number, "forced": False})
        return None, None

    deferred = []
    for item in ranked:  # pass 1: rank order, within kind shares
        prep = prepare(item)
        if prep is None:
            continue
        reason, by = try_add(prep, 1, True)
        if reason == "overlaps_included":
            excluded.append(_excluded(item, reason, by=by))
        elif reason:
            deferred.append((prep, reason))
    for prep, reason in deferred:  # pass 2: leftover budget, rank order, shares ignored
        if not packing["redistribute_unused"]:
            excluded.append(_excluded(prep["item"], reason, tokens=prep["tokens"]))
            continue
        again, by = try_add(prep, 2, False)
        if again == "overlaps_included":
            excluded.append(_excluded(prep["item"], again, by=by))
        elif again:
            excluded.append(_excluded(prep["item"], "budget", tokens=prep["tokens"]))

    def join(preps):
        ordered = sorted(preps, key=lambda p: rank_key(p["item"], ranking, changed_files))
        return SEPARATOR.join(p["block"] for p in ordered)

    text = join(chosen)
    total = count_tokens(text) if chosen else 0
    while total > budget:  # token merges at block edges can still add a few tokens
        removable = [p for p in chosen if not p["forced"]]
        if not removable:
            break
        dropped = removable[-1]  # the item added last is the marginal one
        chosen = [c for c in chosen if c is not dropped]
        excluded.append(_excluded(dropped["item"], "budget", tokens=dropped["tokens"]))
        text = join(chosen)
        total = count_tokens(text) if chosen else 0

    sections = []
    pr_section = render_pr(pr, inputs["strip_html_comments"]) if inputs["pr_text"] else ""
    diff_section = render_diff(trace["diff"]) if inputs["diff"] else ""
    sections += [s for s in (pr_section, diff_section) if s]
    if text:
        sections.append(f"{CONTEXT_HEADING}{SEPARATOR}{text}")
    full = "\n\n".join(sections)

    included = []
    for position, prep in enumerate(sorted(chosen, key=lambda p: rank_key(p["item"], ranking, changed_files)), 1):
        item, segments = prep["item"], prep["segments"]
        shown = sum(b - a + 1 for a, b in segments)
        included.append({"rank": position, "pass": prep["pass"], "forced": prep["forced"], "kind": item.kind,
                         "hop": item.hop, "qn": item.qn, "label": item.label, "file": item.file,
                         "start": prep["start"], "end": prep["end"], "segments": [list(s) for s in segments],
                         "lines_shown": shown, "truncated": shown < prep["end"] - prep["start"] + 1,
                         "tokens": prep["tokens"], "edge": item.edge, "via": item.via,
                         "relations": list(item.relations)})

    reasons = Counter(e["reason"].split(":")[0] if e["reason"].startswith("label:") else e["reason"]
                      for e in excluded)
    report = {
        "render_version": RENDER_VERSION,
        "level": cfg["level"],
        "config_sha256": config_sha256(cfg),
        "parent_config_sha256": parent_report["config_sha256"] if parent_report else None,
        "config": cfg,
        "tokenizer": tokenizer_name,
        "token_budget": budget,
        "input_tokens": count_tokens(full) if full else 0,
        "pr_tokens": count_tokens(pr_section) if pr_section else 0,
        "diff_tokens": count_tokens(diff_section) if diff_section else 0,
        "context_tokens": total,
        "tokens_by_kind": {kind: sum(i["tokens"] for i in included if i["kind"] == kind) for kind in KINDS},
        "kind_caps": caps,
        "pr": ({"title": pr.get("title"), "edited_after_review": pr.get("edited_after_review")}
               if inputs["pr_text"] else None),
        "trace": {k: trace[k] for k in ("tool_version", "repo", "base", "head", "merge_base")},
        "counts": {
            "changed_files": len(trace["changed_files"]),
            "seeds": len(trace["seeds"]), "callers_found": len(trace["callers"]),
            "callees_found": len(trace["callees"]),
            "included_by_kind": dict(Counter(i["kind"] for i in included)),
            "excluded_by_reason": dict(sorted(reasons.items())),
        },
        "included": included,
        "excluded": sorted(excluded, key=lambda e: (e["reason"], e.get("kind") or "", e.get("hop") or 0,
                                                    e.get("qn") or "")),
        "warnings": trace.get("warnings", []),
    }
    return full, report


def shown_lines(report: dict) -> set[tuple[str, int]]:
    """Every (file, line) a report's context shows."""
    return {(i["file"], line) for i in report["included"] for a, b in i["segments"] for line in range(a, b + 1)}


def nesting_check(parent: dict, child: dict) -> dict:
    """Whether everything the parent level shows is also in the child level."""
    missing = shown_lines(parent) - shown_lines(child)
    inputs_p, inputs_c = parent["config"]["input"], child["config"]["input"]
    return {"parent": parent["level"]["name"], "missing_lines": len(missing),
            "pr_text_kept": inputs_c["pr_text"] or not inputs_p["pr_text"],
            "diff_kept": inputs_c["diff"] or not inputs_p["diff"],
            "ok": not missing and (inputs_c["pr_text"] or not inputs_p["pr_text"])
            and (inputs_c["diff"] or not inputs_p["diff"])}
