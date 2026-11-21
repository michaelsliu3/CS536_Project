#!/usr/bin/env python3
"""Record what Codex CLI reads before it reviews a hunk, and make it a context level
(src/harness_context/codex.py).

    .venv/bin/python scripts/harness/record_codex.py check
    .venv/bin/python scripts/harness/record_codex.py checkout --limit 20
    .venv/bin/python scripts/harness/record_codex.py run --limit 20 --keep-repos
    .venv/bin/python scripts/harness/record_codex.py build
    OUTCOME_PROFILE=cloud-ccrb-hunk-codex .venv/bin/python scripts/outcome/run_reviews.py

check     the tool, its settings and, when it answers, the model server (a plain reply and a tool call)
checkout  fetch and check out the chosen hunks' PRs ahead of time (needs GitHub, not the model server)
run    for each chosen hunk: check the PR out at the reviewed commit, run Codex on it read-only,
       and save its event stream to ROOT/harness/codex/<hunk>/ (resumes; needs the model server)
build  turn the saved streams into the level HC1_codex -> ROOT/context_levels.codex.jsonl

ROOT is the folder of the profile's context file. Codex runs with a home folder of its own
(ROOT/harness/codex_home, settings from configs/codex_harness/config.toml) and an empty HOME, so
your own ~/.codex and home folder are not used. Its sandbox lets commands read but not write, and
gives them no network. Read-only still means the agent can read any file this computer lets you
read: `build` leaves out the output of any command that names a path outside the checkout.
"""
from __future__ import annotations

import argparse
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
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "scripts" / "outcome"))

from run_reviews import profile_cells, select_cells  # noqa: E402 - the outcome scripts' cell choice
from src.harness_context import codex as harness  # noqa: E402
from src.review_outcomes import table  # noqa: E402
from src.review_outcomes.profiles import dataset_path, read_notebook_profile  # noqa: E402
from src.review_outcomes.prompts import load_review_prompt  # noqa: E402

CODEX_VERSION = "0.171.0"
CODEX = PROJECT_ROOT / "data" / "tools" / f"codex-cli-{CODEX_VERSION}" / "node_modules" / ".bin" / "codex"
SETTINGS = PROJECT_ROOT / "configs" / "codex_harness" / "config.toml"
SERVE_FLAGS = "--reasoning-parser qwen3 --enable-auto-tool-choice --tool-call-parser qwen3_coder"


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Setup:
    """Where things live for one profile, and the Codex home prepared for its runs."""

    def __init__(self, profile: str, binary: Path):
        self.config = read_notebook_profile(profile)
        self.contexts = PROJECT_ROOT / self.config["contexts"]
        self.dataset = dataset_path(self.config)
        self.root = self.contexts.parent
        self.out = self.root / "harness" / "codex"
        self.home = self.root / "harness" / "codex_home"
        self.empty_home = self.root / "harness" / "empty_home"
        self.repos = self.root / ".work" / "harness_repos"
        self.binary = binary
        self.settings = tomllib.loads(SETTINGS.read_text(encoding="utf-8"))

    def prepare_home(self) -> str:
        """Copy the settings into the Codex home; their sha256."""
        for folder in (self.home, self.empty_home, self.out):
            folder.mkdir(parents=True, exist_ok=True)
        text = SETTINGS.read_text(encoding="utf-8")
        (self.home / "config.toml").write_text(text, encoding="utf-8")
        return sha256(text)

    def env(self) -> dict:
        keep = {k: os.environ[k] for k in ("PATH", "LANG", "LC_ALL", "TMPDIR") if k in os.environ}
        key = self.settings["model_providers"][self.settings["model_provider"]].get("env_key", "VLLM_API_KEY")
        return {**keep, "HOME": str(self.empty_home), "CODEX_HOME": str(self.home), key: os.environ.get(key, "dummy"),
                "TERM": "dumb", "NO_COLOR": "1"}

    def version(self) -> str:
        result = subprocess.run([str(self.binary), "--version"], capture_output=True, text=True, env=self.env(), timeout=60)
        return result.stdout.strip() or result.stderr.strip()

    def base_url(self) -> str:
        return self.settings["model_providers"][self.settings["model_provider"]]["base_url"].rstrip("/")


def run_codex(setup: Setup, repo: Path, prompt: str, out_dir: Path, timeout: int, extra: tuple[str, ...] = ()) -> dict:
    """One `codex exec` on `repo`; its events go to out_dir/events.jsonl as they arrive."""
    out_dir.mkdir(parents=True, exist_ok=True)
    command = [str(setup.binary), "exec", "--json", "--color", "never", "--sandbox", "read-only", "--skip-git-repo-check",
               "--cd", str(repo), "--output-last-message", str(out_dir / "last_message.txt"), *extra, "-"]
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
    setup = Setup(args.profile, args.codex)
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
    with tempfile.TemporaryDirectory() as empty:
        result = run_codex(setup, Path(empty), "Reply with the single word OK.", setup.out / "_check", 120,
                           ("--strict-config", "-c", "model_providers.vllm.request_max_retries=0",
                            "-c", "model_providers.vllm.stream_max_retries=0"))
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


# ---------- run ----------

def choose(cells: list, references: dict, hunks: list[str] | None, limit: int | None) -> list:
    """The hunks to record: the named ones, or every hunk; with `limit`, one PR after another in
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


def chosen_cells(setup: Setup, args) -> list:
    cells = select_cells(profile_cells(setup.config, setup.contexts, setup.dataset, [harness.PARENT]), args.first, args.entries)
    return choose(cells, table.references_by_entry(setup.dataset, cells), args.hunks, args.limit)


def cmd_checkout(args) -> int:
    """Fetch and check out the chosen hunks' PRs now, so `run` does not wait on GitHub while the
    model server is up. A repository folder holds one PR's checkout at a time: with several PRs
    of one repository only the last stays checked out, and `run` switches again as it goes."""
    from src.tracer_context.aacr import load_entries, prepare_checkout
    from src.tracer_context.dataset import load_records

    setup = Setup(args.profile, args.codex)
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


def cmd_run(args) -> int:
    from src.tracer_context.aacr import BASE_BRANCH, load_entries, prepare_checkout
    from src.tracer_context.dataset import load_records

    setup = Setup(args.profile, args.codex)
    settings_sha = setup.prepare_home()
    chosen = chosen_cells(setup, args)
    prompt = load_review_prompt(setup.config.get("prompt", "codex"))
    version, model = setup.version(), setup.settings["model"]

    def job(cell) -> dict:
        request = "\n\n".join(m["content"] for m in prompt.messages(cell.text, cell.parts))
        text = harness.agent_prompt(request, BASE_BRANCH)
        key = harness.settings_key({"prompt": sha256(text), "codex": version, "settings": settings_sha, "model": model})
        return {"cell": cell, "prompt": text, "key": key, "dir": setup.out / cell.entry}

    def saved(item: dict) -> dict | None:
        path = item["dir"] / "run.json"
        run = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
        return run if run and run.get("key") == item["key"] and run.get("status") == "ok" else None

    jobs = [job(cell) for cell in chosen]
    todo = [item for item in jobs if args.force or not saved(item)]
    by_pr: dict[str, list] = defaultdict(list)
    for item in todo:
        by_pr[item["cell"].pr].append(item)
    print(f"{version}, model {model} at {setup.base_url()}\n{len(chosen)} hunks in {len({c.pr for c in chosen})} PRs chosen; "
          f"{len(jobs) - len(todo)} already recorded, {len(todo)} to record in {len(by_pr)} PRs; output {setup.out}")
    if args.dry_run:
        for item in todo:
            print(f"  {item['cell'].entry}  ({item['cell'].file})")
        print("nothing was run")
        return 0
    ok, _ = _http(f"{setup.base_url()}/models", timeout=10)
    if not ok:
        raise SystemExit(f"the model server at {setup.base_url()} does not answer; run `check` first")

    records = load_records(setup.contexts)
    entries = {entry["id"]: entry["record"] for entry in load_entries(setup.dataset)}
    counts: Counter = Counter()
    lock = threading.Lock()
    done = [0]

    def one(pr: str, repo: Path, item: dict) -> dict:
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        result = run_codex(setup, repo, item["prompt"], item["dir"], args.timeout)
        events = harness.parse_events((item["dir"] / "events.jsonl").read_text(encoding="utf-8").splitlines())
        status = ("timeout" if result["timed_out"] else "ok" if result["exit_code"] == 0 and events["turns"] and not events["errors"]
                  else "failed")
        run = {"hunk": item["cell"].entry, "pr": pr, "key": item["key"], "status": status, **result, "started_at": started_at,
               "codex": version, "model": model, "settings_sha256": settings_sha, "repo": str(repo),
               "commit": entries[pr]["target_commit"], "turns": events["turns"], "commands": len(events["commands"]),
               "usage": events["usage"], "errors": events["errors"][:3], "final_message_chars": len(events["final_message"])}
        (item["dir"] / "run.json").write_text(json.dumps(run, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
        with lock:
            counts[status] += 1
            done[0] += 1
            print(f"[{done[0]}/{len(todo)}] {run['hunk']}: {status} in {run['seconds']}s, {run['commands']} commands, "
                  f"{run['usage'].get('input_tokens', 0):,} input tokens" + (f" | {run['errors'][0][:120]}" if run["errors"] else ""),
                  flush=True)
        return run

    def repository(prs: list[str]) -> None:
        """One repository's PRs, one after another: they share a checkout folder."""
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
                list(pool.map(lambda item: one(pr, repo, item), by_pr[pr]))
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


# ---------- build ----------

def cmd_build(args) -> int:
    from src.tracer_context.dataset import load_records
    from src.tracer_context.runtime import make_counter

    setup = Setup(args.profile, args.codex)
    count, label = make_counter(setup.config["reviewer"]["model"])
    limit = setup.config["reviewer"]["max_input_tokens"]
    out_records, stats, totals = [], [], Counter()
    for record in load_records(setup.contexts).values():
        hunks = []
        for hunk in record["hunks"]:
            folder = setup.out / hunk["id"]
            run = json.loads((folder / "run.json").read_text(encoding="utf-8")) if (folder / "run.json").is_file() else None
            if not run or run["status"] != "ok":
                totals["not recorded" if not run else run["status"]] += 1
                continue
            events = harness.parse_events((folder / "events.jsonl").read_text(encoding="utf-8").splitlines())
            parent = hunk["levels"][harness.PARENT]
            text, counts = harness.render_commands(events["commands"], count, limit - (parent["input_tokens"] or 0) - 64, run["repo"])
            info = {"key": run["key"], "tool": run["codex"], "model": run["model"], "turns": run["turns"], "usage": run["usage"],
                    "seconds": run["seconds"], "final_message_chars": run["final_message_chars"]}
            level = harness.harness_level(parent, text, counts, count, limit, info)
            hunks.append({**{k: v for k, v in hunk.items() if k != "levels"}, "levels": {harness.PARENT: parent, harness.LEVEL: level}})
            totals["recorded"] += 1
            totals.update({f"commands {name}": value for name, value in counts.items() if name != "commands"})
            stats.append((counts["included"], level["input_tokens"] or 0, run["usage"].get("input_tokens", 0), level["same_as_parent"]))
        if hunks:
            out_records.append({**record, "hunks": hunks, "harness": {"level": harness.LEVEL, "version": harness.HARNESS_VERSION,
                                                                      "tokenizer": label}})
    path = setup.root / "context_levels.codex.jsonl"
    with path.open("w", encoding="utf-8") as handle:
        for record in out_records:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    print(f"{dict(totals)}\nwrote {path}: {len(stats)} hunks in {len(out_records)} PRs")
    if stats:
        middle = lambda values: sorted(values)[len(values) // 2]  # noqa: E731
        print(f"per hunk, median: {middle([s[0] for s in stats])} commands kept, {middle([s[1] for s in stats]):,} input tokens in the "
              f"level, {middle([s[2] for s in stats]):,} input tokens spent by the session; {sum(s[3] for s in stats)} sessions read nothing")
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)

    def command(name: str, run, text: str):
        sub = commands.add_parser(name, help=text)
        sub.add_argument("--profile", default=os.environ.get("OUTCOME_PROFILE", "cloud-ccrb-hunk"), help="a per-hunk notebook profile")
        sub.add_argument("--codex", type=Path, default=CODEX, help="the Codex CLI program")
        sub.set_defaults(run=run)
        return sub

    command("check", cmd_check, "check the tool, its settings and the model server")

    def choice(sub):
        sub.add_argument("--limit", type=int, help="this many hunks, spread over the PRs")
        sub.add_argument("--hunks", nargs="+", metavar="ID", help="only these hunks")
        sub.add_argument("--entries", nargs="+", metavar="ID", help="only these PRs")
        sub.add_argument("--first", type=int, help="only the first N PRs of the context file")
        return sub

    choice(command("checkout", cmd_checkout, "check the chosen hunks' PRs out ahead of a run"))
    sub = choice(command("run", cmd_run, "record Codex sessions"))
    sub.add_argument("--parallel", type=int, default=6, help="repositories at a time (default 6); keep parallel x concurrency at or "
                     "below the model server's --max-num-seqs (8)")
    sub.add_argument("--concurrency", type=int, default=1, help="sessions at a time within one PR (default 1)")
    sub.add_argument("--timeout", type=int, default=900, help="seconds before a session is stopped (default 900)")
    sub.add_argument("--keep-repos", action="store_true", help="keep each repository checkout after its PR")
    sub.add_argument("--force", action="store_true", help="record again what is already recorded")
    sub.add_argument("--dry-run", action="store_true", help="list the hunks that would be recorded and stop")
    command("build", cmd_build, "write the recorded sessions as a context level")
    args = parser.parse_args(argv)
    return args.run(args)


if __name__ == "__main__":
    sys.exit(main())
