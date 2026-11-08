"""Turn a codebase-memory-mcp trace into a ranked, token-budgeted review context.

Pure functions over the trace dict written by ``src.tracer_context.cbm.trace`` and the
files of the checked-out PR head. No model or tool calls happen here; token counting
is supplied by the caller.
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

KINDS = ("changed", "callee", "caller", "test", "doc")
RANK_KEYS = ("hop", "kind", "edge", "same_file", "size")
OVERSIZE_POLICIES = ("hunks", "head", "skip")
RENDER_VERSION = 1
SEPARATOR = "\n\n"  # between items in the context text

# Expected config shape: a type, a tuple of allowed values, or a nested dict.
_OPT_INT = (int, type(None))
SCHEMA = {
    "version": int,
    "tracer": {
        "binary": str,
        "workers": int,
        "index_mode": ("full", "moderate", "fast"),
        "callers": {"depth": int, "edge_types": list},
        "callees": {"depth": int},
    },
    "selection": {
        "seed_scope": ("innermost", "all"),
        "drop_labels": list,
        "drop_builtins": bool,
        "test_globs": list,
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
        "hunk_context_lines": int,
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


def validate_config(cfg: dict) -> dict:
    """Reject missing or unknown keys and bad values, so typos never pass silently."""
    _check(cfg, SCHEMA, "config")
    if cfg["version"] != 1:
        raise ValueError("config.version must be 1")
    for key in ("workers",):
        if cfg["tracer"][key] < 1:
            raise ValueError(f"config.tracer.{key} must be >= 1")
    for walk in ("callers", "callees"):
        if not 1 <= cfg["tracer"][walk]["depth"] <= 15:
            raise ValueError(f"config.tracer.{walk}.depth must be 1-15")
    limits = cfg["selection"]["limits"]
    if set(limits) != set(KINDS):
        raise ValueError(f"config.selection.limits must have exactly {list(KINDS)}")
    for kind, limit in limits.items():
        _check(limit, {"max_hop": _OPT_INT, "max_items": _OPT_INT}, f"config.selection.limits.{kind}")
        for key, value in limit.items():
            if value is not None and value < 0:
                raise ValueError(f"config.selection.limits.{kind}.{key} must be >= 0 or null")
    ranking = cfg["ranking"]
    if not set(ranking["rank_by"]) <= set(RANK_KEYS) or len(set(ranking["rank_by"])) != len(ranking["rank_by"]):
        raise ValueError(f"config.ranking.rank_by: use distinct keys from {list(RANK_KEYS)}")
    if sorted(ranking["kind_order"]) != sorted(KINDS):
        raise ValueError(f"config.ranking.kind_order must list each of {list(KINDS)} once")
    packing = cfg["packing"]
    for key in ("oversize", "max_lines_per_item"):
        if "default" not in packing[key] or not set(packing[key]) <= set(KINDS) | {"default"}:
            raise ValueError(f"config.packing.{key} needs 'default' and only kind names as keys")
    if not all(v in OVERSIZE_POLICIES for v in packing["oversize"].values()):
        raise ValueError(f"config.packing.oversize values must be in {list(OVERSIZE_POLICIES)}")
    if not all(isinstance(v, int) and not isinstance(v, bool) and v >= 1
               for v in packing["max_lines_per_item"].values()):
        raise ValueError("config.packing.max_lines_per_item values must be integers >= 1")
    shares = packing["kind_shares"]
    if not set(shares) <= set(KINDS):
        raise ValueError(f"config.packing.kind_shares keys must be in {list(KINDS)}")
    for kind, share in shares.items():
        if share is not None and (isinstance(share, bool) or not isinstance(share, (int, float))
                                  or not 0 <= share <= 1):
            raise ValueError(f"config.packing.kind_shares.{kind} must be between 0 and 1, or null")
    if sum(s for s in shares.values() if s is not None) > 1 + 1e-9:
        raise ValueError("config.packing.kind_shares must add up to at most 1")
    for key in ("token_budget", "hunk_context_lines"):
        if packing[key] < 0:
            raise ValueError(f"config.packing.{key} must be >= 0")
    return cfg


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
    return any(glob_to_regex(g).match(path) for g in globs)


def parse_hunks(diff_text: str) -> dict[str, list[list[int]]]:
    """New-side changed line ranges per file from `git diff -U0`. Pure deletions become a
    one-line marker at the following line."""
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


def collect_items(trace: dict, selection: dict) -> tuple[list[Item], list[dict]]:
    """Seeds, callers and callees as typed items; drop noise; merge duplicates."""
    nodes, excluded = trace["nodes"], []
    seeds = [Item(s["qn"], s["label"], s["file"], int(s["start"]), int(s["end"]), "changed", 0)
             for s in trace["seeds"]]
    if selection["seed_scope"] == "innermost":
        kept = []
        for seed in seeds:
            inner = sorted(t.qn for t in seeds if t is not seed and t.file == seed.file
                           and seed.start <= t.start and t.end <= seed.end
                           and (t.start, t.end) != (seed.start, seed.end))
            if inner:
                excluded.append(_excluded(seed, "contains_changed", by=inner[0]))
            else:
                kept.append(seed)
        seeds = kept
    seed_qns = {s["qn"] for s in trace["seeds"]}
    drop_labels = set(selection["drop_labels"])
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

    # One item per symbol: keep the closest relation, remember every kind it had.
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
            parts.append(-1 if item.kind == "changed" else
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


def choose_segments(start, end, kind, file_hunks, packing):
    """Line segments to show for an item, or None when an oversize item is skipped."""
    limit = packing["max_lines_per_item"].get(kind, packing["max_lines_per_item"]["default"])
    if end - start + 1 <= limit:
        return [(start, end)]
    policy = packing["oversize"].get(kind, packing["oversize"]["default"])
    if policy == "skip":
        return None
    if policy == "hunks":
        ctx = packing["hunk_context_lines"]
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
    title = item.kind if item.kind == "changed" else f"{item.kind} (hop {item.hop})"
    header = f"### {title}: {item.qn}  [{item.file}:{start}-{end}]"
    if item.via and item.kind == "callee":
        header += f"  called by {item.via}"
    elif item.via:
        header += f"  {item.edge or 'LINKS'} {item.via}"
    return header


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
    longest = max((len(run) for run in re.findall(r"`+", code)), default=0)
    fence = "`" * max(3, longest + 1)
    language = LANG_BY_EXT.get(Path(item.file).suffix.lower(), "")
    return f"{_header(item, start, end)}\n{fence}{language}\n{code}\n{fence}"


def build_context(trace: dict, repo: Path, cfg: dict, count_tokens: Callable[[str], int],
                  tokenizer_name: str) -> tuple[str, dict]:
    """Return (context text, selection report) for one traced PR."""
    selection, ranking, packing = cfg["selection"], cfg["ranking"], cfg["packing"]
    changed_files = set(trace["changed_files"])
    hunks = trace["hunks"]
    items, excluded = collect_items(trace, selection)
    ranked, limited = apply_limits(items, selection["limits"], ranking, changed_files)
    excluded += limited

    file_cache: dict[str, list[str] | None] = {}
    decorator_cache: dict[str, dict[int, int]] = {}

    def read(path):
        if path not in file_cache:
            full = (repo / path).resolve()
            ok = full.is_file() and repo.resolve() in full.parents
            file_cache[path] = full.read_text(encoding="utf-8", errors="replace").splitlines() if ok else None
        return file_cache[path]

    def prepare(rank, item):
        """Rendered block for an item, or None after recording why it cannot be shown."""
        lines = read(item.file)
        if lines is None:
            excluded.append(_excluded(item, "missing_file"))
            return None
        start, end = max(1, item.start), min(item.end, len(lines))
        if end < start:
            excluded.append(_excluded(item, "bad_range"))
            return None
        if packing["include_decorators"] and item.label != "Section":
            if item.file.endswith(".py") and item.file not in decorator_cache:
                decorator_cache[item.file] = python_decorator_starts("\n".join(lines))
            start = annotation_start(lines, start, item.file, decorator_cache.get(item.file))
        if packing["extend_doc_sections"] and item.label == "Section":
            end = max(end, markdown_section_end(lines, start) or end)
        segments = choose_segments(start, end, item.kind, hunks.get(item.file, []), packing)
        if segments is None:
            excluded.append(_excluded(item, "oversize"))
            return None
        block = render_item(item, lines, segments, start, end, packing["line_numbers"])
        shown = sum(b - a + 1 for a, b in segments)
        # Count the block with the separator that follows it, so the sum tracks the joined text.
        return {"rank": rank, "item": item, "block": block, "tokens": count_tokens(block + SEPARATOR),
                "start": start, "end": end, "lines_shown": shown}

    budget = packing["token_budget"]
    shares = packing["kind_shares"]
    caps = {kind: None if shares.get(kind) is None else int(budget * shares[kind]) for kind in KINDS}
    chosen, deferred, packed = [], [], {}
    used, used_by_kind = 0, Counter()

    def overlapping(prep):
        """An already-chosen item sharing lines with this one (nested classes/methods), if any."""
        item = prep["item"]
        return next((qn for (s, e, qn) in packed.get(item.file, [])
                     if s <= prep["end"] and prep["start"] <= e), None)

    def add(prep, pass_number):
        nonlocal used
        item = prep["item"]
        used += prep["tokens"]
        used_by_kind[item.kind] += prep["tokens"]
        packed.setdefault(item.file, []).append((prep["start"], prep["end"], item.qn))
        chosen.append({**prep, "pass": pass_number})

    for rank, item in enumerate(ranked, 1):  # pass 1: rank order, within kind shares
        prep = prepare(rank, item)
        if prep is None:
            continue
        other = overlapping(prep)
        if other:
            excluded.append(_excluded(item, "overlaps_included", by=other))
        elif caps[item.kind] is not None and used_by_kind[item.kind] + prep["tokens"] > caps[item.kind]:
            deferred.append((prep, "kind_share"))
        elif used + prep["tokens"] > budget:
            deferred.append((prep, "budget"))
        else:
            add(prep, 1)
    for prep, reason in deferred:  # pass 2: leftover budget, rank order, shares ignored
        other = overlapping(prep)
        if other:
            excluded.append(_excluded(prep["item"], "overlaps_included", by=other))
        elif packing["redistribute_unused"] and used + prep["tokens"] <= budget:
            add(prep, 2)
        else:
            excluded.append(_excluded(prep["item"], reason if not packing["redistribute_unused"] else "budget",
                                      tokens=prep["tokens"]))

    def join(preps):
        return SEPARATOR.join(p["block"] for p in sorted(preps, key=lambda p: p["rank"]))

    text = join(chosen)
    total = count_tokens(text) if chosen else 0
    while chosen and total > budget:  # token merges at block edges can still add a few tokens
        dropped = chosen.pop()  # the item added last is the marginal one
        excluded.append(_excluded(dropped["item"], "budget", tokens=dropped["tokens"]))
        text = join(chosen)
        total = count_tokens(text) if chosen else 0
    chosen.sort(key=lambda p: p["rank"])
    included = []
    for prep in chosen:
        item = prep["item"]
        included.append({"rank": prep["rank"], "pass": prep["pass"], "kind": item.kind, "hop": item.hop,
                         "qn": item.qn, "label": item.label, "file": item.file, "start": prep["start"],
                         "end": prep["end"], "lines_shown": prep["lines_shown"],
                         "truncated": prep["lines_shown"] < prep["end"] - prep["start"] + 1,
                         "tokens": prep["tokens"], "edge": item.edge, "via": item.via,
                         "relations": list(item.relations)})

    reasons = Counter(e["reason"].split(":")[0] if e["reason"].startswith("label:") else e["reason"]
                      for e in excluded)
    report = {
        "render_version": RENDER_VERSION,
        "config_sha256": config_sha256(cfg),
        "config": cfg,
        "tokenizer": tokenizer_name,
        "token_budget": budget,
        "context_tokens": total,
        "tokens_by_kind": {kind: sum(i["tokens"] for i in included if i["kind"] == kind) for kind in KINDS},
        "kind_caps": caps,
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
    return text, report
