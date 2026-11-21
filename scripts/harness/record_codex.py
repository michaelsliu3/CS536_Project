#!/usr/bin/env python3
"""Run Codex CLI as a reviewer on the profile's pull requests and score its own reviews
(src/harness_context/codex.py). Codex is the real coding agent the context selector is compared with.

    .venv/bin/python scripts/harness/record_codex.py check
    .venv/bin/python scripts/harness/record_codex.py checkout
    .venv/bin/python scripts/harness/record_codex.py hunks --keep-repos
    .venv/bin/python scripts/harness/record_codex.py review --keep-repos
    .venv/bin/python scripts/harness/record_codex.py score --dry-run
    .venv/bin/python scripts/harness/record_codex.py score

check     the tool, its settings and, when it answers, the model server (a plain reply and a tool call)
checkout  fetch and check out the chosen PRs ahead of time (needs GitHub, not the model server)
hunks     the comparison, one session per hunk the profile reviews: Codex gets the reviewer's own request
          for the hunk and leave to look around the repository first, and answers in the reviewer's
          format. Same unit, request, parser and judge as the project's reviewer; only the agent differs.
          Saved under ROOT/harness/codex/<hunk>/ (resumes; needs the model server)
review    as a developer would run it, one session per PR: Codex's own review command on the checkout.
          Codex gets its own request to review the changes against the base branch plus the PR's title
          and description, and reads the diff and anything else by itself. Saved under
          ROOT/harness/codex_pr/<pr>/ (resumes; needs the model server)
score     read Codex's own reviews from the saved sessions, judge them with the profile's judge and write
          reviews/, judgments/, table.csv, hunk_table.csv, coverage.csv and comparison.json under
          <out_root>/<run_name>_codex/ (needs the judge's server; --dry-run reads and reports only)

The per-hunk run is the comparison because the unit of review decides most of the recall: one review
call per PR covered 6 to 7% of the reference comments in the project's first run, one call per hunk 47
to 63% (docs/per_hunk_review_test.md). The per-PR run is reported next to it.

ROOT is the folder of the profile's context file. Each session folder holds Codex's event stream
(events.jsonl), its final message, a copy of its session files (session_*.jsonl: every message, tool
call and token count) and a summary (run.json). Nothing Codex read is given to the project's reviewer.

Codex runs with a home folder of its own (ROOT/harness/codex_home, settings from
configs/codex_harness/config.toml) and an empty HOME, so your own ~/.codex and home folder are not
used. Its sandbox lets commands read but not write, and gives them no network. Read-only still means
the agent can read any file this computer lets you read, and what it reads goes to the model server:
each session's summary counts the commands that named a path outside the checkout.
"""
from __future__ import annotations

import argparse
import csv
import functools
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from dataclasses import replace
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "outcome"))

from run_reviews import make_reviewer, profile_cells, select_cells  # noqa: E402 - the outcome scripts' own
from src.harness_context import codex as harness  # noqa: E402
from src.review_outcomes import backends, table  # noqa: E402
from src.review_outcomes.profiles import dataset_path, read_notebook_profile  # noqa: E402
from src.review_outcomes.prompts import load_review_prompt  # noqa: E402

CODEX_VERSION = "0.171.0"
CODEX = PROJECT_ROOT / "data" / "tools" / f"codex-cli-{CODEX_VERSION}" / "node_modules" / ".bin" / "codex"
SETTINGS = PROJECT_ROOT / "configs" / "codex_harness" / "config.toml"
SERVE_FLAGS = "--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@functools.lru_cache(maxsize=1)
def real_git_folder() -> str | None:
    """On macOS, the folder of the real git program, to put first on Codex's PATH. /usr/bin/git
    is a launcher that writes a cache file when it starts; in the read-only sandbox it cannot,
    and every git command would print several warning lines into what the model reads."""
    if sys.platform != "darwin":
        return None
    found = subprocess.run(["xcrun", "-f", "git"], capture_output=True, text=True)
    return str(Path(found.stdout.strip()).parent) if found.returncode == 0 and found.stdout.strip() else None


class Setup:
    """Where things live for one profile, and the Codex home prepared for its runs."""

    def __init__(self, args):
        self.config = read_notebook_profile(args.profile)
        self.contexts = PROJECT_ROOT / self.config["contexts"]
        self.dataset = dataset_path(self.config)
        self.root = self.contexts.parent
        harness_dir = Path(args.harness_dir).resolve() if args.harness_dir else self.root / "harness"
        self.out = harness_dir / "codex"        # one folder per hunk session
        self.out_pr = harness_dir / "codex_pr"  # one folder per PR review session
        self.home = harness_dir / "codex_home"
        self.empty_home = harness_dir / "empty_home"
        self.repos = self.root / ".work" / "harness_repos"
        self.binary = args.codex
        self.settings = tomllib.loads(SETTINGS.read_text(encoding="utf-8"))
        self.provider = self.settings["model_provider"]
        self.url = (args.base_url or self.settings["model_providers"][self.provider]["base_url"]).rstrip("/")
        # A server address given on the command line goes to Codex as a setting of this run.
        self.overrides = ("-c", f"model_providers.{self.provider}.base_url={json.dumps(self.url)}") if args.base_url else ()

    def prepare_home(self) -> str:
        """Copy the settings into the Codex home; their sha256."""
        for folder in (self.home, self.empty_home, self.out, self.out_pr):
            folder.mkdir(parents=True, exist_ok=True)
        text = SETTINGS.read_text(encoding="utf-8")
        (self.home / "config.toml").write_text(text, encoding="utf-8")
        return sha256(text)

    def env(self) -> dict:
        keep = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
        if real_git_folder():
            keep["PATH"] = real_git_folder() + os.pathsep + keep.get("PATH", "")
        key = self.settings["model_providers"][self.provider].get("env_key", "VLLM_API_KEY")
        return {**keep, "HOME": str(self.empty_home), "CODEX_HOME": str(self.home), key: os.environ.get(key, "dummy"),
                "TERM": "dumb", "NO_COLOR": "1"}

    def version(self) -> str:
        result = subprocess.run([str(self.binary), "--version"], capture_output=True, text=True, env=self.env(), timeout=60)
        return result.stdout.strip() or result.stderr.strip()

    def base_url(self) -> str:
        return self.url


def run_codex(setup: Setup, repo: Path, prompt: str, out_dir: Path, timeout: int, extra: tuple[str, ...] = (),
              review: bool = False) -> dict:
    """One `codex exec` on `repo`, or one `codex exec review` with `prompt` as the review's
    instructions; its events go to out_dir/events.jsonl as they arrive."""
    out_dir.mkdir(parents=True, exist_ok=True)
    command = [str(setup.binary), "exec", "--json", "--color", "never", "--sandbox", "read-only", "--skip-git-repo-check",
               "--cd", str(repo), "--output-last-message", str(out_dir / "last_message.txt"), *setup.overrides, *extra,
               *(("review", "-") if review else ("-",))]
    started, timed_out = time.monotonic(), False
    with (out_dir / "events.jsonl").open("w", encoding="utf-8") as events, (out_dir / "stderr.log").open("w", encoding="utf-8") as errors:
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=events, stderr=errors, env=setup.env(), text=True)
        try:
            process.communicate(prompt, timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            process.communicate()
    return {"exit_code": process.returncode, "timed_out": timed_out, "seconds": round(time.monotonic() - started, 1)}


def collect_sessions(home: Path, thread_id: str | None, out_dir: Path, since: float) -> list[dict]:
    """Copy the session files Codex wrote for this run (those of the conversation `thread_id`,
    changed since `since`) into out_dir as session_<source>.jsonl, and return them parsed."""
    for old in out_dir.glob("session_*.jsonl"):
        old.unlink()
    sessions = []
    for path in sorted((home / "sessions").rglob("rollout-*.jsonl")) if thread_id else ():
        if path.stat().st_mtime < since - 5:
            continue
        with path.open(encoding="utf-8") as handle:
            first = handle.readline()
        try:
            meta = json.loads(first).get("payload") or {}
        except json.JSONDecodeError:
            continue
        if thread_id not in (meta.get("id"), meta.get("session_id"), meta.get("parent_thread_id")):
            continue
        session = harness.parse_session(path.read_text(encoding="utf-8").splitlines())
        name = f"session_{session['source'] or 'unknown'}"
        target = out_dir / (f"{name}.jsonl" if not (out_dir / f"{name}.jsonl").exists() else f"{name}_{len(sessions)}.jsonl")
        shutil.copyfile(path, target)
        sessions.append(session)
    return sessions


def load_sessions(folder: Path) -> list[dict]:
    return [harness.parse_session(path.read_text(encoding="utf-8").splitlines()) for path in sorted(folder.glob("session_*.jsonl"))]


# ---------- check ----------

def _http(url: str, payload: dict | None = None, timeout: int = 120) -> tuple[bool, object]:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", "Authorization": "Bearer dummy"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return False, f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:300]}"
    except Exception as error:  # noqa: BLE001 - reported, not raised
        return False, f"{type(error).__name__}: {error}"


def cmd_check(args) -> int:
    setup = Setup(args)
    problems = 0

    def report(ok: bool | None, what: str, detail: str = "") -> None:
        nonlocal problems
        problems += ok is False
        print(f"  {'ok  ' if ok else 'wait' if ok is None else 'FAIL'}  {what}" + (f"\n          {detail}" if detail else ""))

    print("tool and settings:")
    if not setup.binary.exists():
        report(False, f"Codex CLI at {setup.binary}",
               f"install it: npm install --prefix {setup.binary.parents[2]} --ignore-scripts @openai/codex@{CODEX_VERSION}")
        return 1
    version = setup.version()
    report(CODEX_VERSION in version, f"Codex CLI: {version}", "" if CODEX_VERSION in version else f"the event format was read for {CODEX_VERSION}")
    setup.prepare_home()
    report(True, f"Codex home {setup.home} (your own ~/.codex is not used)")
    if sys.platform == "darwin":
        report(bool(real_git_folder()), f"git for the sandbox: {real_git_folder() or 'not found with `xcrun -f git`'}",
               "" if real_git_folder() else "every git command Codex runs will print macOS warnings into what the model reads")
    with tempfile.TemporaryDirectory() as empty:
        run_codex(setup, Path(empty), "Reply with the single word OK.", setup.out / "_check", 120,
                  ("--strict-config", "-c", f"model_providers.{setup.provider}.request_max_retries=0",
                   "-c", f"model_providers.{setup.provider}.stream_max_retries=0"))
    events = harness.parse_events((setup.out / "_check" / "events.jsonl").read_text(encoding="utf-8").splitlines())
    stderr = (setup.out / "_check" / "stderr.log").read_text(encoding="utf-8")
    started = events["thread_id"] is not None
    report(started, "settings accepted and a session starts", "" if started else stderr.strip()[-600:])
    for notice in dict.fromkeys(n for n in events["notices"] if "Reconnecting" not in n):
        print(f"          note from Codex: {notice[:200]}")

    print("model server:")
    base, model = setup.base_url(), setup.settings["model"]
    ok, models = _http(f"{base}/models", timeout=10)
    if not ok:
        report(None, f"{base} does not answer ({models}); the rest needs the server",
               f"start vLLM with tool calling on: the usual serve command plus  {SERVE_FLAGS}")
        return 1 if problems else 0
    served = [m.get("id") for m in models.get("data", [])]
    report(model in served, f"serves {served}", "" if model in served else f"the settings name {model!r}")
    ok, reply = _http(f"{base}/responses", {"model": model, "input": "Reply with the single word OK.", "max_output_tokens": 2000})
    report(ok, "Responses API answers", "" if ok else str(reply))
    tool = {"type": "function", "name": "shell", "description": "Run a shell command and return its output.",
            "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}
    ok, reply = _http(f"{base}/responses", {"model": model, "input": "List the files here: call the shell tool with the command ls.",
                                           "tools": [tool], "tool_choice": "auto", "max_output_tokens": 2000})
    called = ok and any(item.get("type") == "function_call" for item in reply.get("output", []))
    report(called, "the model calls a tool through the Responses API",
           "" if called else f"start vLLM with  {SERVE_FLAGS}  (reply: {str(reply)[:300]})")
    if events["turns"]:
        report(True, f"Codex got a reply from the model ({events['usage'].get('input_tokens', 0):,} input tokens for a one-word answer)")
    else:
        report(None, "Codex itself was tried before the server answered: run `check` again to see a full session")
    return 1 if problems else 0


# ---------- choosing and running sessions ----------

def choose(cells: list, references: dict, hunks: list[str] | None, limit: int | None) -> list:
    """The hunks to run: the named ones, or every hunk; with `limit`, one PR after another in
    turn, each PR's hunks that carry a reference comment first."""
    if hunks:
        unknown = sorted(set(hunks) - {c.entry for c in cells})
        if unknown:
            raise SystemExit(f"not among the profile's hunks: {', '.join(unknown)}")
        return [c for c in cells if c.entry in set(hunks)]
    if not limit:
        return cells
    by_pr: dict[str, list] = defaultdict(list)
    for cell in cells:
        by_pr[cell.pr].append(cell)
    for pr, group in by_pr.items():
        comments = [c for c in references[pr]["comments"] if c.get("note")]
        group.sort(key=lambda cell: not any(table._in_hunk(c, cell) for c in comments))  # stable: diff order within each kind
    chosen, depth = [], 0
    while len(chosen) < limit and any(depth < len(g) for g in by_pr.values()):
        chosen += [g[depth] for g in by_pr.values() if depth < len(g)][:limit - len(chosen)]
        depth += 1
    return chosen


def request_cells(setup: Setup) -> list:
    """The profile's hunks, each with the reviewer's own request for it (the no-context level)."""
    return profile_cells(setup.config, setup.contexts, setup.dataset, [harness.REQUEST_LEVEL])


def chosen_cells(setup: Setup, args) -> list:
    cells = select_cells(request_cells(setup), args.first, args.entries)
    return choose(cells, table.references_by_entry(setup.dataset, cells), getattr(args, "hunks", None), getattr(args, "limit", None))


def cmd_checkout(args) -> int:
    """Fetch and check out the chosen PRs now, so a run does not wait on GitHub while the model
    server is up. A repository folder holds one PR's checkout at a time: with several PRs of one
    repository only the last stays checked out, and a run switches again as it goes."""
    from src.tracer_context.aacr import load_entries, prepare_checkout
    from src.tracer_context.dataset import load_records

    setup = Setup(args)
    prs = list(dict.fromkeys(cell.pr for cell in chosen_cells(setup, args)))
    records = load_records(setup.contexts)
    entries = {entry["id"]: entry["record"] for entry in load_entries(setup.dataset)}
    failed = 0
    for number, pr in enumerate(prs, 1):
        started = time.monotonic()
        try:
            checkout = prepare_checkout(setup.repos, entries[pr], records[pr]["merge_base"])
            print(f"[{number}/{len(prs)}] {pr}: at {entries[pr]['target_commit'][:10]} in {time.monotonic() - started:.0f}s "
                  f"({', '.join(checkout['fetch_log']) or 'already fetched'})", flush=True)
        except Exception as error:  # noqa: BLE001 - reported; the other PRs are still prepared
            failed += 1
            print(f"[{number}/{len(prs)}] {pr}: FAILED: {type(error).__name__}: {str(error)[:200]}", flush=True)
    print(f"{len(prs) - failed} of {len(prs)} PRs checked out under {setup.repos}; run with --keep-repos to keep using them")
    return 1 if failed else 0


def saved_run(folder: Path, key: str | None = None) -> dict | None:
    """A session's summary when the session finished with a review (and, with `key`, was made
    with the same prompt, tool, settings and model)."""
    path = folder / "run.json"
    run = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    return run if run and run.get("status") == "ok" and key in (None, run.get("key")) else None


def record(setup: Setup, jobs: list[dict], args, what: str) -> int:
    """Run Codex once per job, a job being {id, pr, review, prompt(checkout) -> text, dir}.
    Repositories run in parallel; the PRs of one repository one after another, because they
    share a checkout folder."""
    from src.tracer_context.aacr import load_entries, prepare_checkout
    from src.tracer_context.dataset import load_records

    settings_sha = setup.prepare_home()
    version, model = setup.version(), setup.settings["model"]
    records = load_records(setup.contexts)
    entries = {entry["id"]: entry["record"] for entry in load_entries(setup.dataset)}
    for job in jobs:
        job["text"] = job["prompt"](records[job["pr"]]["merge_base"])
        job["key"] = harness.settings_key({"prompt": sha256(job["text"]), "review": job["review"], "codex": version,
                                           "settings": settings_sha, "model": model})
    todo = [job for job in jobs if args.force or not saved_run(job["dir"], job["key"])]
    by_pr: dict[str, list] = defaultdict(list)
    for job in todo:
        by_pr[job["pr"]].append(job)
    print(f"{version}, model {model} at {setup.base_url()}\n{len(jobs)} {what} in {len({j['pr'] for j in jobs})} PRs chosen; "
          f"{len(jobs) - len(todo)} already done, {len(todo)} to run in {len(by_pr)} PRs; output {jobs[0]['dir'].parent if jobs else ''}")
    if args.dry_run:
        for job in todo:
            print(f"  {job['id']}  ({len(job['text']):,} characters sent)")
        if todo and args.show_prompt:
            print(f"--- what Codex gets for {todo[0]['id']} ---\n{todo[0]['text']}\n---")
        print("nothing was run")
        return 0
    ok, _ = _http(f"{setup.base_url()}/models", timeout=10)
    if not ok:
        raise SystemExit(f"the model server at {setup.base_url()} does not answer; run `check` first")

    counts: Counter = Counter()
    lock = threading.Lock()
    done = [0]

    def one(repo: Path, job: dict) -> None:
        started_at, since = datetime.now(timezone.utc).isoformat(timespec="seconds"), time.time()
        result = run_codex(setup, repo, job["text"], job["dir"], args.timeout, review=job["review"])
        events = harness.parse_events((job["dir"] / "events.jsonl").read_text(encoding="utf-8").splitlines())
        sessions = collect_sessions(setup.home, events["thread_id"], job["dir"], since)
        totals = harness.session_totals(sessions)
        reply = harness.review_reply(sessions) if job["review"] else events["final_message"]
        status = ("timeout" if result["timed_out"] else "ok" if result["exit_code"] == 0 and events["turns"] and not events["errors"]
                  and reply.strip() else "failed")
        run = {"id": job["id"], "pr": job["pr"], "mode": "pr" if job["review"] else "hunk", "key": job["key"], "status": status,
               **result, "started_at": started_at, "codex": version, "model": model, "settings_sha256": settings_sha,
               "repo": str(repo), "commit": entries[job["pr"]]["target_commit"], "merge_base": records[job["pr"]]["merge_base"],
               "turns": events["turns"], "requests": totals["requests"], "commands": len(totals["commands"] or events["commands"]),
               "commands_outside": sum(harness.leaves_repository(c["command"], str(repo)) for c in totals["commands"] or events["commands"]),
               "usage": totals["usage"] or events["usage"], "session_files": len(sessions), "errors": events["errors"][:3],
               "reply_chars": len(reply)}
        (job["dir"] / "run.json").write_text(json.dumps(run, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        with lock:
            counts[status] += 1
            done[0] += 1
            print(f"[{done[0]}/{len(todo)}] {run['id']}: {status} in {run['seconds']}s, {run['requests']} model requests, "
                  f"{run['commands']} commands, {run['usage'].get('input_tokens', 0):,} input tokens"
                  + (f" | {run['errors'][0][:120]}" if run["errors"] else ""), flush=True)

    def repository(prs: list[str]) -> None:
        for pr in prs:
            try:
                checkout = prepare_checkout(setup.repos, entries[pr], records[pr]["merge_base"])
            except Exception as error:  # noqa: BLE001 - one PR that cannot be checked out must not stop the run
                with lock:
                    counts["checkout failed"] += len(by_pr[pr])
                    print(f"{pr}: checkout failed: {type(error).__name__}: {str(error)[:200]}", flush=True)
                continue
            repo = Path(checkout["repo"])
            with ThreadPoolExecutor(max_workers=max(1, args.concurrency)) as pool:
                list(pool.map(lambda job: one(repo, job), by_pr[pr]))
            if not args.keep_repos:
                shutil.rmtree(repo, ignore_errors=True)

    by_repo: dict[str, list[str]] = defaultdict(list)
    for pr in by_pr:
        by_repo[pr.rsplit("__", 1)[0]].append(pr)
    print(f"{len(by_repo)} repositories, up to {args.parallel} at a time, up to {args.concurrency} sessions per PR", flush=True)
    with ThreadPoolExecutor(max_workers=max(1, args.parallel)) as pool:
        list(pool.map(repository, by_repo.values()))
    print(f"done: {dict(counts)}")
    return 0 if set(counts) <= {"ok"} else 1


def cmd_review(args) -> int:
    """As a developer would run it: Codex's own review command, once per PR."""
    from src.tracer_context.aacr import BASE_BRANCH

    setup = Setup(args)
    first_cell = {}
    for cell in select_cells(request_cells(setup), args.first, args.entries):
        first_cell.setdefault(cell.pr, cell)  # the PR's title and description are the same in each of its hunks
    jobs = [{"id": pr, "pr": pr, "review": True, "dir": setup.out_pr / pr,
             "prompt": lambda merge_base, cell=cell: harness.review_instructions(
                 BASE_BRANCH, merge_base, cell.parts.pr_title, cell.parts.pr_description)}
            for pr, cell in first_cell.items()]
    return record(setup, jobs, args, "PR reviews")


def cmd_hunks(args) -> int:
    """The comparison: Codex reviews one hunk with the reviewer's own request, after looking around."""
    from src.tracer_context.aacr import BASE_BRANCH

    setup = Setup(args)
    prompt = load_review_prompt(setup.config.get("prompt", "codex"))

    def text(cell) -> str:
        return harness.agent_prompt("\n\n".join(m["content"] for m in prompt.messages(cell.text, cell.parts)), BASE_BRANCH)

    jobs = [{"id": cell.entry, "pr": cell.pr, "review": False, "dir": setup.out / cell.entry,
             "prompt": lambda merge_base, cell=cell: text(cell)} for cell in chosen_cells(setup, args)]
    return record(setup, jobs, args, "hunk reviews")


# ---------- score ----------

def read_reviews(setup: Setup) -> tuple[list[dict], list[dict], list, Counter]:
    """(PR review records, hunk review records, the hunk records' cells, session counts) from the
    saved sessions that finished with a review and were made with the settings file as it is now:
    sessions made under other settings are counted and left out, so one table never mixes them."""
    from src.tracer_context.dataset import load_records

    counts: Counter = Counter()
    settings_sha = sha256(SETTINGS.read_text(encoding="utf-8"))

    def usable(folder: Path, kind: str) -> dict | None:
        if not (folder / "run.json").is_file():
            return None
        run = saved_run(folder)
        if run and run.get("settings_sha256") != settings_sha:
            counts[f"{kind} sessions made with other settings (left out)"] += 1
            return None
        counts[f"{kind} sessions ok" if run else f"{kind} sessions not ok"] += 1
        return run

    records = load_records(setup.contexts)
    pr_reviews = []
    for pr in records:
        folder = setup.out_pr / pr
        run = usable(folder, "PR")
        if run:
            text = harness.review_reply(load_sessions(folder))
            parsed = harness.pr_review(text, run["repo"], records[pr].get("known_paths", ()))
            pr_reviews.append(harness.review_record(pr, harness.PR_LEVEL, text, parsed, run))
    hunk_reviews, cells = [], []
    for cell in request_cells(setup):
        folder = setup.out / cell.entry
        run = usable(folder, "hunk")
        if run:
            events = harness.parse_events((folder / "events.jsonl").read_text(encoding="utf-8").splitlines())
            parsed = harness.hunk_review(events["final_message"], cell.file, cell.parts.diff if cell.parts else None)
            hunk_reviews.append(harness.review_record(cell.entry, harness.HUNK_LEVEL, events["final_message"], parsed, run,
                                                      pr=cell.pr, file=cell.file))
            cells.append(replace(cell, level=harness.HUNK_LEVEL, input_tokens=(run.get("usage") or {}).get("input_tokens")))
    return pr_reviews, hunk_reviews, cells, counts


def describe_reviews(name: str, reviews: list[dict]) -> str:
    if not reviews:
        return f"{name}: none"
    statuses = Counter(review["status"] for review in reviews)
    total = lambda field: sum((review["session"]["usage"] or {}).get(field, 0) for review in reviews)  # noqa: E731
    return (f"{name}: {len(reviews)} reviews, statuses {dict(sorted(statuses.items()))}, "
            f"{sum(len(r['parsed']['findings']) for r in reviews)} findings; per session on average "
            f"{total('input_tokens') // len(reviews):,} input tokens ({total('cached_input_tokens') // len(reviews):,} cached), "
            f"{total('output_tokens') // len(reviews):,} output, {sum(r['session']['requests'] or 0 for r in reviews) / len(reviews):.1f} "
            f"model requests, {sum(r['session']['commands'] or 0 for r in reviews) / len(reviews):.1f} commands, "
            f"{sum(r['session']['seconds'] or 0 for r in reviews) / len(reviews):.0f}s")


def in_reference_files(judgment: dict, comments: list[dict]) -> int:
    """How many of a review's findings sit in a file that has a reference comment. The
    project's per-hunk runs review only the hunks in such files, so a precision over these
    findings is the one comparable with theirs."""
    files = {table.normalize_separators(c.get("path")) for c in comments if c.get("note")}
    return sum(1 for item in judgment["records"] if item["path"] in files)


def _recall(levels: dict, expected: int) -> None:
    for level in levels.values():
        level["recall"] = None if level.get("covered") is None or not expected else round(level["covered"] / expected, 4)
        if level.get("generated"):
            level["precision"] = round(level["matched_generated"] / level["generated"], 4)


def hunk_comparison(setup: Setup, rows: list[dict], cells: list) -> dict:
    """Codex per hunk next to the project's levels on the same hunks: the hunks Codex reviewed,
    each level's saved reviews and judgments of exactly those hunks (from the profile's own run
    folder) put together per PR as the table does, and the input tokens of both. A level counts
    only when every one of those hunks has a judged review at it."""
    codex_rows = [row for row in rows if row["level"] == harness.HUNK_LEVEL and "recall" in row]
    ids = {cell.entry for cell in cells}
    out = {"hunks": len(ids), "prs": len(codex_rows), "levels": {}}
    if not codex_rows:
        return out
    total = lambda name: sum(row.get(name) or 0 for row in codex_rows)  # noqa: E731
    out["references"] = total("expected")
    out["levels"][harness.HUNK_LEVEL] = {"covered": total("covered"), "generated": total("generated"),
                                         "matched_generated": total("matched_generated"), "input_tokens": total("input_tokens"),
                                         "output_tokens": total("output_tokens")}
    main = PROJECT_ROOT / setup.config["out_root"] / setup.config["run_name"]
    ours = [cell for cell in profile_cells(setup.config, setup.contexts, setup.dataset, setup.config["levels"]) if cell.entry in ids]
    references = table.references_by_entry(setup.dataset, ours)
    for level in setup.config["levels"]:
        group = [cell for cell in ours if cell.level == level]
        saved = lambda kind: [record for cell in group  # noqa: E731
                              if (record := table.read_json(main / kind / cell.entry / f"{level}.json"))]
        reviews, judgments = saved("reviews"), saved("judgments")
        entry = {"covered": None, "input_tokens": sum(cell.input_tokens or 0 for cell in group),
                 "output_tokens": sum((review.get("reply") or {}).get("completion_tokens") or 0 for review in reviews) or None}
        if group and len(judgments) == len(group):
            pr_rows = table.build_hunk_tables(group, reviews, judgments, references)[0]
            entry.update(covered=sum(row.get("covered") or 0 for row in pr_rows), generated=sum(row.get("generated") or 0 for row in pr_rows),
                         matched_generated=sum(row.get("matched_generated") or 0 for row in pr_rows))
        out["levels"][level] = entry
    _recall(out["levels"], out["references"])
    return out


def pr_comparison(setup: Setup, rows: list[dict]) -> dict:
    """Codex's PR reviews next to the project's levels on the same PRs: references covered, and
    the input tokens of reviewing every hunk of those PRs at each level (from the context file,
    whether or not the level's run reviewed every hunk). Coverage of the levels comes from the
    profile's own run folder, when it has a table."""
    codex_rows = {row["entry"]: row for row in rows if row["level"] == harness.PR_LEVEL and "recall" in row}
    prs = set(codex_rows)
    out = {"prs": len(prs), "levels": {}}
    if not prs:
        return out
    total = lambda name: sum(row.get(name) or 0 for row in codex_rows.values())  # noqa: E731
    out["references"] = total("expected")
    out["levels"][harness.PR_LEVEL] = {
        "covered": total("covered"), "generated": total("generated"), "matched_generated": total("matched_generated"),
        "generated_in_reference_files": total("generated_in_reference_files"), "input_tokens": total("input_tokens"),
        "cached_input_tokens": total("cached_input_tokens"), "output_tokens": total("output_tokens")}
    tokens: dict[str, int] = defaultdict(int)
    for cell in table.discover_cells(setup.contexts, entries=prs, levels=setup.config["levels"]):  # every hunk, no filter
        tokens[cell.level] += cell.input_tokens or 0
    covered: dict[str, int] = {}
    path = PROJECT_ROOT / setup.config["out_root"] / setup.config["run_name"] / "table.csv"
    if path.is_file():
        with path.open(newline="", encoding="utf-8") as handle:
            judged: dict[str, set] = defaultdict(set)
            for row in csv.DictReader(handle):
                if row["entry"] in prs and row.get("covered") not in (None, ""):
                    covered[row["level"]] = covered.get(row["level"], 0) + int(row["covered"])
                    judged[row["level"]].add(row["entry"])
        covered = {level: count for level, count in covered.items() if judged[level] == prs}  # only a level judged on all of them
    for level in setup.config["levels"]:
        out["levels"][level] = {"covered": covered.get(level), "input_tokens": tokens.get(level)}
    _recall(out["levels"], out["references"])
    return out


def write_scores(setup: Setup, out: Path, pr_reviews: list[dict], hunk_reviews: list[dict], cells: list, judge,
                 concurrency: int = 1) -> tuple[list[dict], dict, int]:
    """Judge the reviews and write the run folder `out`; (table rows, the two comparisons, failed judge calls)."""
    reviews = pr_reviews + hunk_reviews
    references = table.references_by_entry(setup.dataset, cells)
    for review in reviews:
        table.write_json(out / "reviews" / review["entry"] / f"{review['level']}.json", review)
    judgments = table.run_judgments(reviews, references, judge, out, concurrency=concurrency)
    rows, coverage = table.build_table(pr_reviews, judgments, references)
    judged = {(j["entry"], j["level"]): j for j in judgments}
    for row, review in zip(rows, sorted(pr_reviews, key=lambda r: (r["entry"], r["level"]))):
        usage, judgment = review["session"]["usage"] or {}, judged.get((row["entry"], row["level"]))
        row.update(cached_input_tokens=usage.get("cached_input_tokens"), reasoning_output_tokens=usage.get("reasoning_output_tokens"),
                   requests=review["session"]["requests"], commands=review["session"]["commands"],
                   commands_outside=review["session"]["commands_outside"])
        if judgment:
            row["generated_in_reference_files"] = in_reference_files(judgment, references[row["entry"]]["comments"])
    hunk_rows: list[dict] = []
    if cells:
        per_hunk_rows, hunk_rows, per_hunk_coverage = table.build_hunk_tables(cells, hunk_reviews, judgments, references)
        rows, coverage = per_hunk_rows + rows, per_hunk_coverage + coverage
    table.write_outputs(out, rows, coverage, hunk_rows)
    summary = {"per_hunk": hunk_comparison(setup, rows, cells), "per_pr": pr_comparison(setup, rows)}
    table.write_json(out / "comparison.json", summary)
    return rows, summary, sum(j["judge_errors"] for j in judgments)


def cmd_score(args) -> int:
    setup = Setup(args)
    config = setup.config
    out = PROJECT_ROOT / config["out_root"] / (config["run_name"] + "_codex")
    pr_reviews, hunk_reviews, cells, counts = read_reviews(setup)
    print(f"sessions: {dict(counts) or 'none saved yet'}\n{describe_reviews('per hunk', hunk_reviews)}\n"
          f"{describe_reviews('per PR', pr_reviews)}\nrun folder {out}")
    if args.dry_run or not pr_reviews + hunk_reviews:
        print("nothing was judged or written")
        return 0
    spec = config["judge"]
    table.start_run_log(out, "judgments")
    rows, summary, errors = write_scores(setup, out, pr_reviews, hunk_reviews, cells, backends.judge_caller(make_reviewer(spec)),
                                         args.concurrency or spec.get("concurrency", 1))

    def show(levels: dict) -> None:
        for name, level in levels.items():
            print(f"  {name}: " + ("not judged on all of them" if level["covered"] is None else
                                   f"covered {level['covered']} (recall {level['recall']}"
                                   + (f", precision {level['precision']}" if "precision" in level else "") + ")")
                  + f", input tokens {level['input_tokens'] or 0:,}")

    per_hunk, per_pr = summary["per_hunk"], summary["per_pr"]
    if per_hunk["prs"]:
        print(f"the comparison: on the {per_hunk['hunks']} hunks Codex reviewed, in {per_hunk['prs']} PRs with "
              f"{per_hunk['references']} reference comments:")
        show(per_hunk["levels"])
    if per_pr["prs"]:
        print(f"as a developer would run it: on the {per_pr['prs']} PRs Codex reviewed whole ({per_pr['references']} reference "
              f"comments; the levels' tokens are for every hunk of those PRs):")
        show(per_pr["levels"])
    print(f"wrote {out}" + (f"; {errors} judge calls failed: run `score` again to retry them" if errors else ""))
    return 1 if errors else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, run, text: str):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--profile", default=os.environ.get("OUTCOME_PROFILE", "cloud-ccrb-hunk"), help="a per-hunk notebook profile")
        sub.add_argument("--codex", type=Path, default=CODEX, help="the Codex CLI program")
        sub.add_argument("--base-url", metavar="URL", help="the model server, when it is not at the address in the settings file")
        sub.add_argument("--harness-dir", metavar="DIR", help="where sessions and the Codex home are kept (default ROOT/harness); "
                         "give another folder for a rehearsal against scripts/harness/fake_model_server.py")
        sub.set_defaults(run=run)
        return sub

    def prs(sub):
        sub.add_argument("--entries", nargs="+", metavar="ID", help="only these PRs")
        sub.add_argument("--first", type=int, help="only the first N PRs of the context file")
        return sub

    def hunks(sub):
        sub.add_argument("--limit", type=int, help="this many hunks, spread over the PRs")
        sub.add_argument("--hunks", nargs="+", metavar="ID", help="only these hunks")
        return prs(sub)

    def running(sub, timeout: int):
        sub.add_argument("--parallel", type=int, default=6, help="repositories at a time (default 6); keep parallel x concurrency at or "
                         "below the model server's --max-num-seqs (8)")
        sub.add_argument("--concurrency", type=int, default=1, help="sessions at a time within one PR (default 1)")
        sub.add_argument("--timeout", type=int, default=timeout, help=f"seconds before a session is stopped (default {timeout})")
        sub.add_argument("--keep-repos", action="store_true", help="keep each repository checkout after its PR")
        sub.add_argument("--force", action="store_true", help="run again what is already done")
        sub.add_argument("--dry-run", action="store_true", help="list the sessions that would run and stop")
        sub.add_argument("--show-prompt", action="store_true", help="with --dry-run: also print what the first session would send")
        return sub

    command("check", cmd_check, "check the tool, its settings and the model server")
    hunks(command("checkout", cmd_checkout, "check the chosen PRs out ahead of a run"))
    running(hunks(command("hunks", cmd_hunks, "the comparison: Codex reviews one hunk at a time, like the reviewer")), 900)
    running(prs(command("review", cmd_review, "as a developer would run it: Codex's own review command, once per PR")), 3600)
    sub = command("score", cmd_score, "judge Codex's own reviews and write the tables")
    sub.add_argument("--concurrency", type=int, help="reviews judged at a time (default: the profile's)")
    sub.add_argument("--dry-run", action="store_true", help="read the saved reviews and report; judge and write nothing")
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
