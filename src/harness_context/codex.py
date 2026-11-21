"""Codex CLI as the comparison for the context selector: a real coding agent reviewing the same
changes on the same model, gathering its own context.

Two runs, both in Codex's read-only sandbox on a checkout of the pull request at the reviewed
commit:

- **Per hunk (the comparison).** Codex reviews one hunk at a time, like the project's reviewer.
  It gets the reviewer's own request for the hunk (the pull request's title and description and
  the hunk's diff) and leave to look around the repository first. It answers in the reviewer's
  format. The unit, the request, the reply format, the parser and the judge are the reviewer's,
  and the one thing that differs is that an agent gathers the context.
- **Per pull request (as a developer would run it).** Codex's own review command. It gets
  Codex's own request to review the changes against the base branch, and the pull request's
  title and description. It reads the diff and whatever else it wants by itself. Its review
  comes back as the JSON of Codex's review rubric, the rubric and parser of the project's first
  run (configs/review_prompts/codex_review_rubric.md, review_outcomes.parse.parse_review).

The per-hunk run is the comparison because the unit of review decides most of the recall: in
the project's own runs, one review call per pull request with that rubric covered 6 to 7% of the
reference comments at every context level, and one call per hunk 47 to 63%
(docs/per_hunk_review_test.md). A per-PR Codex review set against a per-hunk reviewer would
measure that, not the context. The per-PR run is reported next to it as what the tool gives when
used as shipped.

Both are scored on Codex's own final review, with the project's judge. Nothing Codex read is
handed to the project's reviewer, and no context level is made from it: the selector's levels
stay the project's own, and Codex is what they are compared with.

Two files describe a session. `codex exec --json` prints one event per line (the commands, the
final message; fields follow codex-rs/exec/src/exec_events.rs at rust-v0.171.0). Codex also
writes a session file per conversation under its home folder (`sessions/.../rollout-*.jsonl`):
every message, tool call and tool output, and the token counts of each model request. In review
mode the review runs in a conversation of its own, so its reply (the JSON) and its token counts
are only in that session file; the event stream carries a rendered copy of the review and no
token counts.

Nothing here runs Codex or reads files: scripts/harness/record_codex.py does both.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from typing import Iterable

from src.review_outcomes.parse import PARSER_VERSION, TAG_PARSER_VERSION, parse_review, parse_tagged_review

HUNK_LEVEL = "C01_codex_hunk"  # the comparison: Codex's own review of one hunk
PR_LEVEL = "C02_codex_pr"      # as a developer would run it: Codex's own review of the whole pull request
REQUEST_LEVEL = "H01_min"      # the per-hunk request is the reviewer's input with no context
HARNESS_VERSION = "codex-harness-v2"

# Codex's own request for `codex exec review --base <branch>`, word for word
# (codex-rs/prompts/src/review_request.rs at rust-v0.171.0, BASE_BRANCH_PROMPT). The command takes
# either --base or instructions of our own, not both, so the pull request's text can only be
# given by sending this request ourselves with the text after it.
BASE_BRANCH_REVIEW = ("Review the code changes against the base branch '{base}'. The merge base commit for this comparison is "
                      "{merge_base}. Run `git diff {merge_base}` to inspect the changes relative to {base}. Provide prioritized, "
                      "actionable findings.")
PR_TEXT = "The pull request under review:\n\nTitle: {title}\n\nDescription:\n{description}"

PREFACE = """\
You are in a checkout of the repository at the commit under review. The pull request is the \
difference between the branch `{base}` and HEAD.

Before you answer, look around the repository with read-only shell commands and gather whatever \
you need to review the change below well: for example the rest of the file, the definitions it \
calls, the code that calls it, and its tests. Read only files inside this repository. Do not \
modify anything, and do not try to build or run the project.

When you have seen enough, reply with the review, in exactly the format the request below asks \
for.

--------

"""

_SHELLS = {"sh", "bash", "zsh"}
_PROGRAM = re.compile(r"(?:^|(?<=[\s;&|(]))/(?:usr/(?:local/)?|opt/homebrew/)?s?bin/[\w.+-]+")  # a program by its full path
# A system or user folder named as an absolute path, not a folder of that name inside the repository (src/var/).
_OUTSIDE_ROOT = re.compile(r"(?:^|(?<=[\s'\"=<>|;&(]))/(?:etc|Users|home|root|var|private|Volumes|Library|System|tmp|mnt|proc)(?:/|\b)")
_BARE_PATH = re.compile(r"(?:^|(?<=[\s=<>|;&(]))/[^\s'\"|;&)<>]*")  # an absolute path that is not inside quotes
_EXIT_CODE = re.compile(r"^Process exited with code (-?\d+)", re.MULTILINE)
_OUTPUT_TOKENS = re.compile(r"^Original token count: (\d+)", re.MULTILINE)
_USAGE_FIELDS = ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens")


def review_instructions(base: str, merge_base: str, title: str, description: str) -> str:
    """What Codex's review command gets for one pull request: its own base-branch request, then
    the pull request's title and description. The diff is not sent: Codex reads it itself."""
    request = BASE_BRANCH_REVIEW.format(base=base, merge_base=merge_base)
    text = PR_TEXT.format(title=(title or "").strip() or "(none)", description=(description or "").strip() or "(none)")
    return f"{request}\n\n{text}\n"


def agent_prompt(review_request: str, base: str) -> str:
    """The per-hunk prompt: leave to explore, then the reviewer's own request for the hunk."""
    return PREFACE.format(base=base) + review_request


def _json_lines(lines: Iterable[str]) -> Iterable[dict]:
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            yield value


def parse_events(lines: Iterable[str]) -> dict:
    """A `codex exec --json` stream as {thread_id, commands, final_message, turns, usage, errors,
    notices}.

    `commands` are the finished command executions in order: {command, output, exit_code,
    status}. `usage` sums the token counts over the turns (all zero in review mode: see
    parse_session). `errors` are failed turns; `notices` are the messages Codex prints along the
    way (a reconnect, an unknown model name), which do not by themselves mean the session
    failed. Lines that are not JSON are skipped."""
    out = {"thread_id": None, "commands": [], "final_message": "", "turns": 0, "reasoning_items": 0,
           "usage": {}, "errors": [], "notices": []}
    for event in _json_lines(lines):
        kind, item = event.get("type"), event.get("item") or {}
        if kind == "thread.started":
            out["thread_id"] = event.get("thread_id")
        elif kind == "turn.completed":
            out["turns"] += 1
            for name, value in (event.get("usage") or {}).items():
                if isinstance(value, int):
                    out["usage"][name] = out["usage"].get(name, 0) + value
        elif kind == "turn.failed":
            out["errors"].append(str((event.get("error") or {}).get("message") or "turn failed"))
        elif kind == "error" or (kind == "item.completed" and item.get("type") == "error"):
            out["notices"].append(str(event.get("message") or item.get("message") or "error"))
        elif kind == "item.completed" and item.get("type") == "command_execution":
            out["commands"].append({"command": item.get("command") or "", "output": item.get("aggregated_output") or "",
                                    "exit_code": item.get("exit_code"), "status": item.get("status")})
        elif kind == "item.completed" and item.get("type") == "agent_message":
            out["final_message"] = item.get("text") or ""
        elif kind == "item.completed" and item.get("type") == "reasoning":
            out["reasoning_items"] += 1
    return out


def _text(content) -> str:
    return "".join(part.get("text") or "" for part in content or () if isinstance(part, dict))


def parse_session(lines: Iterable[str]) -> dict:
    """One Codex session file as {id, session_id, parent, source, requests, usage, request_usage,
    reply, commands}.

    `source` is "exec" for the conversation `codex exec` started and "review" for the review
    conversation it hands a review to (`parent` is then the first one's id; both carry the same
    `session_id`). `usage` is the conversation's running total after its last model request,
    `request_usage` the counts of each request in order, `reply` the model's last message.
    `commands` are the shell commands the model ran: {command, output, exit_code,
    output_tokens}, where `output` is what the model was shown (Codex's header lines, then the
    command's output, which Codex may have shortened) and `output_tokens` Codex's own count of
    the full output."""
    out = {"id": None, "session_id": None, "parent": None, "source": None, "requests": 0, "usage": {}, "request_usage": [],
           "reply": "", "commands": []}
    calls: dict[str, dict] = {}
    for record in _json_lines(lines):
        kind, payload = record.get("type"), record.get("payload") or {}
        if kind == "session_meta":
            source = payload.get("source")
            out.update(id=payload.get("id"), session_id=payload.get("session_id"), parent=payload.get("parent_thread_id"),
                       source=source.get("subagent") if isinstance(source, dict) else source)
        elif kind == "token_usage_record":
            out["requests"] += 1
            out["request_usage"].append({k: v for k, v in (payload.get("usage") or {}).items() if k in _USAGE_FIELDS})
        elif kind == "event_msg" and payload.get("type") == "token_count":
            total = (payload.get("info") or {}).get("total_token_usage") or {}
            out["usage"] = {k: v for k, v in total.items() if k in _USAGE_FIELDS} or out["usage"]
        elif kind == "response_item" and payload.get("type") == "message" and payload.get("role") == "assistant":
            out["reply"] = _text(payload.get("content")) or out["reply"]
        elif kind == "response_item" and payload.get("type") == "function_call":
            try:
                arguments = json.loads(payload.get("arguments") or "{}")
            except json.JSONDecodeError:
                arguments = {}
            command = arguments.get("cmd") or arguments.get("command") if isinstance(arguments, dict) else None
            if command:
                call = {"command": command if isinstance(command, str) else shlex.join(map(str, command)), "output": "",
                        "exit_code": None, "output_tokens": None}
                calls[payload.get("call_id")] = call
                out["commands"].append(call)
        elif kind == "response_item" and payload.get("type") == "function_call_output" and payload.get("call_id") in calls:
            output = payload.get("output")
            output = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            code, tokens = _EXIT_CODE.search(output), _OUTPUT_TOKENS.search(output)
            calls[payload["call_id"]].update(output=output, exit_code=int(code.group(1)) if code else None,
                                             output_tokens=int(tokens.group(1)) if tokens else None)
    return out


def session_totals(sessions: list[dict]) -> dict:
    """{usage, requests, commands} over a run's conversations: the token counts summed (input
    tokens count every model request's whole input, so text read early is counted again by each
    later request; `cached_input_tokens` is the part of it the server had cached)."""
    usage = {name: sum(s["usage"].get(name, 0) for s in sessions) for name in _USAGE_FIELDS if any(name in s["usage"] for s in sessions)}
    return {"usage": usage, "requests": sum(s["requests"] for s in sessions),
            "commands": [command for s in sessions for command in s["commands"]]}


def review_reply(sessions: list[dict]) -> str:
    """The reply of the review conversation: the JSON Codex's review rubric asks for, as the model
    wrote it. "" when the run has no review conversation."""
    return next((s["reply"] for s in sessions if s["source"] == "review"), "")


def shown_command(command: str) -> str:
    """A command without the shell Codex wraps it in: `/bin/zsh -lc 'rg foo'` is shown as `rg foo`."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return command.strip()
    if len(parts) == 3 and parts[0].rsplit("/", 1)[-1] in _SHELLS and parts[1] in ("-lc", "-c"):
        return parts[2].strip()
    return command.strip()


def leaves_repository(command: str, repo: str = "") -> bool:
    """Whether a command names a place outside the checkout: the home folder, a parent folder, a
    system or user folder, or any other absolute path that is not under `repo`. The sandbox lets
    the agent read any file the computer lets you read, so a run reports how many of its
    commands did this. The test errs on the side of counting a command."""
    shown = shown_command(command)
    if repo:
        shown = shown.replace(repo.rstrip("/"), "REPO")
    shown = _PROGRAM.sub("PROGRAM", shown)
    if "~" in shown or "$HOME" in shown or "${HOME" in shown or re.search(r"(^|[\s'\"=])\.\./", shown):
        return True
    if _OUTSIDE_ROOT.search(shown):
        return True
    return any(path != "/dev/null" for path in _BARE_PATH.findall(shown))


def pr_review(text: str, repo: str, known_paths=()) -> dict:
    """Codex's review of a pull request, parsed like the project's own JSON reviews. Codex names
    files by their full path in the checkout `repo`; they are made repository-relative."""
    parsed = parse_review(text, known_paths)
    root = repo.rstrip("/") + "/"
    for finding in parsed["findings"]:
        raw = (finding.get("raw_path") or "").replace("\\", "/")
        if raw.startswith(root):
            finding["path"] = raw[len(root):]
    return parsed


def hunk_review(text: str, file: str, diff: str | None) -> dict:
    """Codex's review of one hunk, parsed by the reviewer's own rule for a per-hunk review."""
    return parse_tagged_review(text, path=file, diff=diff, on_hunk_only=True)


def settings_key(settings: dict) -> str:
    """One hash of everything that shapes a session: prompt, tool version, model, config."""
    return hashlib.sha256(json.dumps({"version": HARNESS_VERSION, **settings}, sort_keys=True).encode("utf-8")).hexdigest()


def review_record(entry: str, level: str, text: str, parsed: dict, run: dict, **extra) -> dict:
    """Codex's own review as a record the outcome table's judge stage reads
    (review_outcomes.table.run_judgments), from the session's saved summary `run` (run.json).
    `input_tokens` is everything the session sent to the model, over all its requests."""
    usage = run.get("usage") or {}
    parser = PARSER_VERSION if level == PR_LEVEL else f"{TAG_PARSER_VERSION}+on-hunk"
    reply = hashlib.sha256(text.encode("utf-8")).hexdigest()
    return {"key": settings_key({"session": run["key"], "reply": reply, "parser": parser, "level": level}),
            "entry": entry, "level": level, "status": parsed["status"], "parsed": parsed, "truncated": False,
            "input_tokens": usage.get("input_tokens"), "input_sha256": None, "parser": parser,
            "backend": f"{run.get('codex')} with {run.get('model')}",
            "reply": {"text": text, "completion_tokens": usage.get("output_tokens"), "seconds": run.get("seconds")},
            "session": {name: run.get(name) for name in ("usage", "requests", "commands", "commands_outside", "seconds", "commit")},
            **extra}
