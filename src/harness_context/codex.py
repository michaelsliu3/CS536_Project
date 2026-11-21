"""What a real coding agent reads before it reviews a hunk, recorded as one more context level.

Codex CLI (open source) is run on a checkout of the pull request, in its read-only sandbox, and
asked to look around the repository before it reviews one hunk. `codex exec --json` prints one
JSON event per line; the commands the agent ran and what they printed are exactly what the
harness put in front of the model. That text becomes the level `HC1_codex`: the PR text, the
hunk, then each command with its output. It is reviewed by the same one-call reviewer as every
other level, so only the context differs.

It is a comparison level, not one a selector can offer: getting it costs a whole agent session
(`usage` in the level's `harness` entry), far more than the recorded text itself.

Event fields follow codex-rs/exec/src/exec_events.rs at rust-v0.171.0. Nothing here runs Codex
or reads files: scripts/harness/record_codex.py does both.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from typing import Callable, Iterable

from src.tracer_context.build import CONTEXT_HEADING, SEPARATOR, input_sections

LEVEL = "HC1_codex"
PARENT = "H01_min"
HARNESS_VERSION = "codex-harness-v1"

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
_FENCE = re.compile(r"`{3,}")
_PROGRAM = re.compile(r"(?:^|(?<=[\s;&|(]))/(?:usr/(?:local/)?|opt/homebrew/)?s?bin/[\w.+-]+")  # a program by its full path
# A system or user folder named as an absolute path, not a folder of that name inside the repository (src/var/).
_OUTSIDE_ROOT = re.compile(r"(?:^|(?<=[\s'\"=<>|;&(]))/(?:etc|Users|home|root|var|private|Volumes|Library|System|tmp|mnt|proc)(?:/|\b)")
_BARE_PATH = re.compile(r"(?:^|(?<=[\s=<>|;&(]))/[^\s'\"|;&)<>]*")  # an absolute path that is not inside quotes


def agent_prompt(review_request: str, base: str) -> str:
    """The prompt Codex gets for one hunk: leave to explore, then the reviewer's own request."""
    return PREFACE.format(base=base) + review_request


def parse_events(lines: Iterable[str]) -> dict:
    """A `codex exec --json` stream as {thread_id, commands, final_message, turns, usage, errors,
    notices}.

    `commands` are the finished command executions in order: {command, output, exit_code,
    status}. `usage` sums the token counts over the turns. `errors` are failed turns; `notices`
    are the messages Codex prints along the way (a reconnect, an unknown model name), which do
    not by themselves mean the session failed. Lines that are not JSON are skipped."""
    out = {"thread_id": None, "commands": [], "final_message": "", "turns": 0, "reasoning_items": 0,
           "usage": {}, "errors": [], "notices": []}
    for line in lines:
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
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
    system or user folder, or any other absolute path that is not under `repo`. Such output is
    kept out of the level: it is not repository context, and it may be private. The test errs
    on the side of leaving a command out."""
    shown = shown_command(command)
    if repo:
        shown = shown.replace(repo.rstrip("/"), "REPO")
    shown = _PROGRAM.sub("PROGRAM", shown)
    if "~" in shown or "$HOME" in shown or "${HOME" in shown or re.search(r"(^|[\s'\"=])\.\./", shown):
        return True
    if _OUTSIDE_ROOT.search(shown):
        return True
    return any(path != "/dev/null" for path in _BARE_PATH.findall(shown))


def _block(command: str, output: str) -> str:
    longest = max((len(run) for run in _FENCE.findall(output)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"### $ {shown_command(command)}{SEPARATOR}{fence}\n{output.rstrip()}\n{fence}"


def render_commands(commands: list[dict], count_tokens: Callable[[str], int], budget: int, repo: str = "") -> tuple[str, dict]:
    """(the context text, counts) from a session's commands: each command that succeeded and
    printed something, once, in the order it ran, until `budget` tokens are used."""
    blocks, seen, used = [], set(), 0
    counts = {"commands": len(commands), "included": 0, "failed_or_empty": 0, "repeated": 0, "outside_repository": 0, "over_budget": 0}
    for command in commands:
        output = command.get("output") or ""
        if command.get("exit_code") not in (0, None) or command.get("status") not in ("completed", None) or not output.strip():
            counts["failed_or_empty"] += 1
        elif leaves_repository(command["command"], repo):
            counts["outside_repository"] += 1
        elif (shown_command(command["command"]), output) in seen:
            counts["repeated"] += 1
        else:
            seen.add((shown_command(command["command"]), output))
            block = _block(command["command"], output)
            cost = count_tokens(block)
            if counts["over_budget"] or used + cost > budget:
                counts["over_budget"] += 1
                continue
            blocks.append(block)
            used += cost
            counts["included"] += 1
    return SEPARATOR.join(blocks), counts


def settings_key(settings: dict) -> str:
    """One hash of everything that shapes a recording: prompt, tool version, model, config."""
    return hashlib.sha256(json.dumps({"version": HARNESS_VERSION, **settings}, sort_keys=True).encode("utf-8")).hexdigest()


def harness_level(parent: dict, context_text: str, counts: dict, count_tokens: Callable[[str], int], max_input_tokens: int,
                  info: dict) -> dict:
    """The recorded context as a level record in the builder's shape, next to its parent level
    (the hunk with no context). A session that read nothing gives a level equal to its parent."""
    text, sections = parent["text"], parent["sections"]
    part = lambda name: text[sections[name][0]:sections[name][1]] if name in sections else ""  # noqa: E731
    pr_section, diff_section = part("pr"), part("diff")
    level = {"parent": PARENT, "max_input_tokens": max_input_tokens, "pr_tokens": parent.get("pr_tokens"),
             "diff_tokens": parent.get("diff_tokens"), "token_budget": max(0, max_input_tokens - (parent.get("input_tokens") or 0)),
             "fits": True, "capped": bool(counts.get("over_budget")), "items": counts.get("included", 0),
             "included_by_kind": {"command": counts["included"]} if counts.get("included") else {}, "included": [],
             "nesting_ok": None, "config_sha256": info.get("key"), "harness": {**info, "commands": counts}}
    if not context_text:
        return {**level, "text": None, "sections": sections, "same_as_parent": True, "context_tokens": 0,
                "input_tokens": parent.get("input_tokens")}
    full = SEPARATOR.join(p for p in (pr_section, diff_section, CONTEXT_HEADING + SEPARATOR + context_text) if p)
    return {**level, "text": full, "sections": input_sections(pr_section, diff_section, "", context_text),
            "same_as_parent": False, "context_tokens": count_tokens(context_text), "input_tokens": count_tokens(full)}
