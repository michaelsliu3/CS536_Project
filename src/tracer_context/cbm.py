"""Run codebase-memory-mcp on one PR checkout and collect what the context builder needs.

The checkout must have the PR head checked out with a clean worktree; `base` names the
PR's base commit or branch. The tool diffs `base...HEAD` (from their merge base), the
same convention as the AACR-Bench records checked so far.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path, PurePosixPath

from src.tracer_context.build import parse_hunks

TRACE_FORMAT = 2
_BATCH = 200  # qualified names per graph query
_MAX_HITS_KEPT = 200  # per name; the total is still recorded
# Files agents and contributors read for repository conventions.
_GUIDE_NAME = re.compile(r"(?i)^(readme|contributing|agents|claude|gemini|architecture|development|developing|"
                         r"hacking|styleguide|style_guide|coding_style|code_style)(\.(md|markdown|rst|txt|adoc))?$")
_GUIDE_PATH = re.compile(r"^(\.cursorrules|\.github/copilot-instructions\.md|\.cursor/rules/[^/]+\.mdc?)$")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_WORD = re.compile(r"[A-Za-z0-9_]+")  # git grep -w word characters
_NOT_SYMBOLS = {"File", "Module", "Folder", "Project", "Package", "Section"}


class TracerError(RuntimeError):
    pass


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise TracerError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def tracer_settings(tracer_cfg: dict) -> dict:
    """The settings that change what the tool returns (the binary path does not)."""
    return {k: tracer_cfg[k] for k in ("workers", "index_mode", "callers", "callees")}


class _Tool:
    def __init__(self, binary: Path, cache_dir: Path, workers: int):
        self.binary = str(binary)
        self.env = {**os.environ, "CBM_CACHE_DIR": str(cache_dir), "CBM_WORKERS": str(workers)}
        self.env.pop("CBM_RUNTIME_DIR", None)  # long paths break the tool's Unix socket

    def run(self, *args: str, stdin: str | None = None, check: bool = True) -> str:
        result = subprocess.run([self.binary, *args], input=stdin, capture_output=True, text=True,
                                env=self.env, timeout=1800)
        if check and result.returncode != 0:
            raise TracerError(f"codebase-memory-mcp {' '.join(args)} failed ({result.returncode}): "
                              f"{(result.stderr or result.stdout).strip()[:800]}")
        return result.stdout

    def call(self, tool: str, payload: dict) -> dict:
        out = self.run("cli", "--quiet", tool, stdin=json.dumps(payload))
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            raise TracerError(
                f"{tool} did not return JSON: {out.strip()[:800]}\n"
                "If another codebase-memory-mcp session is running with a different cache "
                "(for example the graph UI), stop it and retry.") from None


def _query(tool: _Tool, project: str, cypher: str) -> list[list[str]]:
    result = tool.call("query_graph", {"project": project, "query": cypher, "max_rows": 99998,
                                       "max_output_tokens": 1_000_000, "format": "json"})
    if result.get("has_more") or result.get("truncated"):
        raise TracerError(f"graph query was truncated: {cypher[:200]}")
    return result.get("rows", [])


def _cypher_list(names) -> str:
    return "[" + ", ".join(json.dumps(n) for n in names) + "]"


def _batches(names):
    names = sorted(names)
    for i in range(0, len(names), _BATCH):
        yield names[i:i + _BATCH]


def guide_files(repo: Path) -> list[str]:
    tracked = [p for p in _git(repo, "ls-files", "-z").split("\0") if p]
    return sorted(p for p in tracked if _GUIDE_NAME.match(PurePosixPath(p).name) or _GUIDE_PATH.match(p))


def text_hits(repo: Path, seeds: list[dict]) -> dict:
    """Whole-word hits for each changed symbol's short name in the tracked files, like an
    agent's `grep -w`: one hit per (file, line), binary files skipped, lines numbered as git does.

    Implemented as one pass that splits each file into words and checks them against the set
    of names; `git grep -w` with many patterns took minutes on large vendored C/C++ trees.
    """
    names: dict[str, list[str]] = {}
    for seed in seeds:
        short = seed["qn"].rsplit(".", 1)[-1]
        if seed["label"] not in _NOT_SYMBOLS and _IDENTIFIER.fullmatch(short):
            names.setdefault(short, []).append(seed["qn"])
    found: dict[str, set[tuple[str, int]]] = {name: set() for name in names}
    wanted = set(names)
    if wanted:
        for path in (p for p in _git(repo, "ls-files", "-z").split("\0") if p):
            full = repo / path
            if full.is_symlink() or not full.is_file():  # gitlinks (submodules), links, missing files
                continue
            data = full.read_bytes()
            if b"\0" in data[:8000]:  # binary, as `git grep -I` decides
                continue
            text = data.decode("utf-8", errors="replace")
            present = wanted.intersection(_WORD.findall(text))
            if not present:
                continue
            for number, line in enumerate(text.split("\n"), 1):
                for name in present.intersection(_WORD.findall(line)):
                    found[name].add((path, number))
    return {name: {"total": len(found[name]), "seeds": sorted(names[name]),
                   "hits": [list(h) for h in sorted(found[name])[:_MAX_HITS_KEPT]]} for name in sorted(names)}


def trace(repo: Path, base: str, tracer_cfg: dict, binary: Path, cache_dir: Path) -> dict:
    """Index `repo` into a fresh `cache_dir` and return the trace dict (names without the
    tool's path-derived project prefix, so traces compare across machines)."""
    repo = repo.resolve()
    binary = binary.resolve()
    if not binary.is_file():
        raise TracerError(f"codebase-memory-mcp binary not found: {binary}")
    if _git(repo, "status", "--porcelain", "--untracked-files=no").strip():
        raise TracerError(f"{repo} has uncommitted changes; the tool would mix them into the diff")
    head = _git(repo, "rev-parse", "HEAD").strip()
    merge_base = _git(repo, "merge-base", base, "HEAD").strip()
    hunks = parse_hunks(_git(repo, "diff", "-U0", "--no-color", "--no-ext-diff", merge_base, "HEAD"))
    diff = _git(repo, "diff", "-U3", "--no-color", "--no-ext-diff", merge_base, "HEAD")  # as GitHub shows it
    guides = guide_files(repo)

    if cache_dir.exists():
        shutil.rmtree(cache_dir)
    cache_dir.mkdir(parents=True, mode=0o700)
    tool = _Tool(binary, cache_dir, tracer_cfg["workers"])
    version = tool.run("--version").strip()
    for key in ("watcher_enabled", "auto_watch", "ui_enabled"):
        tool.run("config", "set", key, "false")
    warnings, timings = [], {}
    clock = [time.monotonic()]

    def lap(name):
        now = time.monotonic()
        timings[name] = round(now - clock[0], 2)
        clock[0] = now

    try:
        index = tool.call("index_repository", {"repo_path": str(repo), "mode": tracer_cfg["index_mode"]})
        if index.get("status") != "indexed":
            raise TracerError(f"indexing failed: {json.dumps(index)[:800]}")
        project = index["project"]
        prefix = project + "."
        strip = lambda qn: qn[len(prefix):] if qn and qn.startswith(prefix) else qn  # noqa: E731
        lap("index")

        # The tool rejects link types this repository's graph does not contain (e.g. no TESTS
        # links in a snapshot without tests), so ask only for those present.
        present = {row[0] for row in _query(tool, project, "MATCH (a)-[r]->(b) RETURN type(r), count(r)")}
        callers_cfg, callees_cfg = tracer_cfg["callers"], tracer_cfg["callees"]
        edge_types = [t for t in callers_cfg["edge_types"] if t in present]
        missing = [t for t in callers_cfg["edge_types"] if t not in present]
        if missing:
            warnings.append(f"callers: link types not in this graph, not followed: {', '.join(missing)}")

        common = {"project": project, "base_branch": base, "seeds": "list", "seed_limit": 1000,
                  "limit": 5000, "changed_limit": 5000, "max_output_tokens": 1_000_000, "format": "json"}
        if edge_types:
            callers = tool.call("detect_changes", {**common, "direction": "inbound", "depth": callers_cfg["depth"],
                                                   "edge_types": edge_types, "via": True})
        else:  # nothing to follow: still need the changed files and symbols
            callers = {**tool.call("detect_changes", {**common, "direction": "outbound", "depth": 1}), "impacted": []}
            warnings.append("callers: no requested link type exists in this graph; no callers traced")
        lap("callers")
        callees = tool.call("detect_changes", {**common, "direction": "outbound", "depth": callees_cfg["depth"]})
        lap("callees")
        for name, result in (("callers", callers), ("callees", callees)):
            for flag in ("changed_has_more", "seed_has_more", "impacted_has_more"):
                if result.get(flag):
                    warnings.append(f"{name}: {flag} (tool limit reached; results are incomplete)")
            if result.get("merge_base") != merge_base:
                raise TracerError(f"{name}: tool merge base {result.get('merge_base')} != git {merge_base}")

        seeds = callers["seeds"]
        names = {s["qn"] for s in seeds}
        names |= {r["qn"] for r in callers["impacted"] + callees["impacted"] if r["qn"].startswith(prefix)}
        nodes = {}
        for batch in _batches(names):
            rows = _query(tool, project, f"MATCH (n) WHERE n.qualified_name IN {_cypher_list(batch)} "
                                         "RETURN n.qualified_name, n.label, n.file_path, n.start_line, n.end_line")
            for qn, label, path, start, end in rows:
                nodes[strip(qn)] = {"label": label, "file": path,
                                    "start": int(start) if start else 0, "end": int(end) if end else 0}
        lap("node_ranges")

        # Which callee is called by which changed symbol or nearer callee (for the "called by" note).
        callee_names = {r["qn"] for r in callees["impacted"]}
        sources = {s["qn"] for s in seeds} | {r["qn"] for r in callees["impacted"]
                                              if int(r["hop"]) < callees_cfg["depth"] and r["qn"].startswith(prefix)}
        callee_edges = set()
        for batch in _batches(sources):
            for a, b in _query(tool, project, f"MATCH (a)-[r:CALLS]->(b) WHERE a.qualified_name IN "
                                              f"{_cypher_list(batch)} RETURN a.qualified_name, b.qualified_name"):
                if b in callee_names:
                    callee_edges.add((strip(a), strip(b)))
        lap("callee_links")
    finally:
        tool.run("daemon", "stop", check=False)

    def rows(result, keys):
        out = [{k: (strip(r.get(k)) if k in ("qn", "via_qn") else r.get(k)) for k in keys if k in r}
               for r in result["impacted"]]
        return sorted(out, key=lambda r: (int(r["hop"]), r["qn"]))

    stripped_seeds = sorted(({**s, "qn": strip(s["qn"])} for s in seeds), key=lambda s: (s["file"], s["start"], s["qn"]))
    hits = text_hits(repo, stripped_seeds)
    lap("text_search")
    return {
        "format": TRACE_FORMAT,
        "tool_version": version,
        "repo": str(repo),
        "base": base,
        "head": head,
        "merge_base": merge_base,
        "tracer_settings": tracer_settings(tracer_cfg),
        "index": {k: index.get(k) for k in ("nodes", "edges", "skipped_count", "parse_partial_count",
                                             "parse_unusable_count", "not_indexed_files_count")},
        "changed_files": sorted(callers["changed_files"]),
        "hunks": hunks,
        "diff": diff,
        "guide_files": guides,
        "text_hits": hits,
        "seeds": stripped_seeds,
        "callers": rows(callers, ("qn", "label", "file", "hop", "via_qn", "via_edge")),
        "callees": rows(callees, ("qn", "label", "file", "hop")),
        "nodes": dict(sorted(nodes.items())),
        "callee_edges": sorted([list(e) for e in callee_edges]),
        "warnings": warnings,
        "timings": timings,
    }
