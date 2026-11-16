"""Reviewer and judge prompts.

Reviewer: OpenAI Codex's built-in `/review` rubric, used verbatim as the system message
(configs/review_prompts/codex_review_rubric.md, from openai/codex at commit
4e4767ffa763ec0d0a19e04cf8ff0163a987297b, codex-rs/prompts/templates/review/rubric.md,
Apache-2.0). Codex's own request tells the reviewer to run `git diff`; a one-call reviewer
cannot, so REVIEW_REQUEST adapts Codex's commit-review request to say the material follows.

AACR reviewer (the per-hunk test, docs/per_hunk_review_test.md): the prompt the AACR-Bench paper
uses for its non-agent rows (arXiv 2601.19494v3, appendix Figures 16 and 17, CC BY 4.0), used
verbatim: configs/review_prompts/aacr_nonagent_no_context.md for an input with nothing but the PR
text and the diff, aacr_nonagent_with_context.md when the level shows more. The reviewer answers
in the paper's tags, which parse.parse_tagged_review reads.

Judge: AACR-Bench's file-level semantic judge prompt, copied from alibaba/aacr-bench at
commit ee9d7fef70ef31acea2ff7ae316ce0a9575fe4df, evaluation/judge.py (Apache-2.0).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUBRIC_PATH = PROJECT_ROOT / "configs" / "review_prompts" / "codex_review_rubric.md"
RUBRIC_SHA256 = "ec60e7f36a1d1c2679ce095c0205ecc56f7dd8fb57707a13ef362072390f219f"

# Adapted from Codex's COMMIT_PROMPT ("Review the code changes introduced by commit {{sha}}.
# Provide prioritized, actionable findings."); the material is inline instead of in a repo.
REVIEW_REQUEST = (
    "Review the code changes in this pull request. Its title and description, its diff, and any "
    "repository context are included below; you cannot run commands or open other files. "
    "Provide prioritized, actionable findings."
)
REVIEW_PROMPT_VERSION = "codex-rubric-4e4767f+one-call-request-v1"
# Sent once, after a reply that finished without the JSON (src/review_outcomes/retry.py). It is not
# part of the first call, so it is not in REVIEW_PROMPT_VERSION; the retry policy carries its hash.
FOLLOW_UP_REQUEST = (
    "You cannot run commands or open other files; everything available to you is in the request above. "
    "Give your review now as the JSON object the output format specifies, with no other text. "
    "If you have no findings, return an empty findings list."
)
JUDGE_PROMPT_VERSION = "aacr-file-level-ee9d7fe"


def load_rubric(path: Path = RUBRIC_PATH) -> str:
    text = Path(path).read_text(encoding="utf-8")
    actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if path == RUBRIC_PATH and actual != RUBRIC_SHA256:
        raise ValueError(f"{path} changed (sha256 {actual}); the frozen reviewer prompt must not drift")
    return text


def review_messages(context_text: str, rubric: str) -> list[dict[str, str]]:
    """System = the rubric; user = the request followed by the level's context.md."""
    return [{"role": "system", "content": rubric},
            {"role": "user", "content": f"{REVIEW_REQUEST}\n\n{context_text}"}]


def prompt_sha256(rubric: str) -> str:
    """Identity of everything in the prompt except the per-cell context."""
    return hashlib.sha256(f"{rubric}\0{REVIEW_REQUEST}".encode("utf-8")).hexdigest()


# The retry rule's follow-up for a reply in the paper's tags (retry.py); the Codex one is above.
AACR_FOLLOW_UP_REQUEST = (
    "You cannot run commands or open other files; everything available to you is in the request above. "
    "Give your review now in the format the instructions above specify, with no other text. "
    "If you have no issues to report, output only <end />."
)
AACR_NO_CONTEXT_PATH = PROJECT_ROOT / "configs" / "review_prompts" / "aacr_nonagent_no_context.md"      # Figure 16
AACR_WITH_CONTEXT_PATH = PROJECT_ROOT / "configs" / "review_prompts" / "aacr_nonagent_with_context.md"  # Figure 17
AACR_NO_CONTEXT_SHA256 = "1dba0ebdec40f6f6d9414e9b6dfc27c6e54116c6d91e6b1731767833b0c4ed97"
AACR_WITH_CONTEXT_SHA256 = "a4583087c014439229343ae8b6f45aa67118a29ed364d81064be8074e505e8ee"
AACR_PROMPT_VERSION = "aacr-nonagent-2601.19494v3-fig16-fig17"
_SLOT = re.compile(r"\{\{ (\w+) \}\}")  # the paper's templates mark their slots like {{ pr_title }}
REPLY_FORMATS = ("codex-json", "aacr-tags")


@dataclass(frozen=True)
class ReviewInput:
    """One cell's input in parts, for a prompt with a slot for each (build.split_input)."""
    pr_title: str
    pr_description: str
    diff: str      # the diff under review, without a heading or a fence
    contexts: str  # everything else the level shows; "" when it shows nothing more


@dataclass(frozen=True)
class ReviewPrompt:
    """A reviewer prompt: its identity, the reply format it asks for, and how a cell's input
    becomes chat messages."""
    name: str
    version: str       # saved with every review and part of its cache key
    sha256: str        # of everything in the prompt except the cell's input
    reply_format: str  # one of REPLY_FORMATS: which parser reads the reply
    follow_up: str     # the retry rule's message to a reply that finished without an answer
    build: Callable[[str, ReviewInput | None], list[dict[str, str]]]

    def messages(self, context_text: str, parts: ReviewInput | None = None) -> list[dict[str, str]]:
        """`context_text` is the level's whole text; `parts` the same input taken apart."""
        return self.build(context_text, parts)


def codex_prompt(rubric: str) -> ReviewPrompt:
    """The per-PR reviewer's prompt; the same messages and identity as review_messages / prompt_sha256."""
    return ReviewPrompt("codex", REVIEW_PROMPT_VERSION, prompt_sha256(rubric), "codex-json", FOLLOW_UP_REQUEST,
                        lambda text, parts: review_messages(text, rubric))


def _template(path: Path, pinned_path: Path, pinned_sha256: str, slots: set[str]) -> str:
    text = Path(path).read_text(encoding="utf-8")
    actual = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if path == pinned_path and actual != pinned_sha256:
        raise ValueError(f"{path} changed (sha256 {actual}); a reviewer prompt in use must not drift")
    found = _SLOT.findall(text)
    if set(found) != slots or len(found) != len(slots):
        raise ValueError(f"{path} must have each of the slots {sorted(slots)} once, not {sorted(found)}")
    return text.rstrip("\n")


def aacr_prompt(no_context: Path = AACR_NO_CONTEXT_PATH, with_context: Path = AACR_WITH_CONTEXT_PATH) -> ReviewPrompt:
    """The AACR-Bench paper's prompt for non-agent methods: one user message, with the paper's
    Figure 17 when the input has context beyond the PR text and the diff, and Figure 16 when it
    has none. Needs the input in parts."""
    base = {"pr_title", "pr_description", "diff_hunk"}
    plain = _template(no_context, AACR_NO_CONTEXT_PATH, AACR_NO_CONTEXT_SHA256, base)
    rich = _template(with_context, AACR_WITH_CONTEXT_PATH, AACR_WITH_CONTEXT_SHA256, base | {"contexts"})

    def build(text: str, parts: ReviewInput | None) -> list[dict[str, str]]:
        if parts is None:
            raise ValueError("the AACR prompt needs the input in parts; build the context file again "
                             "(its levels then record where each part is)")
        values = {"pr_title": parts.pr_title, "pr_description": parts.pr_description, "diff_hunk": parts.diff,
                  "contexts": parts.contexts}
        template = rich if parts.contexts else plain
        return [{"role": "user", "content": _SLOT.sub(lambda match: values[match.group(1)], template)}]

    return ReviewPrompt("aacr-hunk", AACR_PROMPT_VERSION, hashlib.sha256(f"{plain}\0{rich}".encode("utf-8")).hexdigest(),
                        "aacr-tags", AACR_FOLLOW_UP_REQUEST, build)


REVIEW_PROMPTS = ("codex", "aacr-hunk")


def load_review_prompt(name: str = "codex") -> ReviewPrompt:
    """The reviewer prompt a run profile names (its "prompt" entry; "codex" when it has none)."""
    if name == "codex":
        return codex_prompt(load_rubric())
    if name == "aacr-hunk":
        return aacr_prompt()
    raise ValueError(f"unknown reviewer prompt {name!r}; use one of {REVIEW_PROMPTS}")


def as_prompt(rubric_or_prompt) -> ReviewPrompt:
    """A ReviewPrompt as it is; a rubric text as the Codex prompt built on it."""
    return rubric_or_prompt if isinstance(rubric_or_prompt, ReviewPrompt) else codex_prompt(rubric_or_prompt)


def judge_prompt(file_path: str, gen_lines: str, gen_note: str, ref_notes: list[str], ref_lines: list[str]) -> str:
    """AACR-Bench's file-level judge prompt, unchanged."""
    numbered_refs = "\n".join(f"{i + 1}. [L{ref_lines[i]}] {note}" for i, note in enumerate(ref_notes))
    return f"""
-Role-

You are an expert code reviewer comparing review comments for semantic equivalence.

-Task-

Given a Generated Comment and a numbered list of Reference Comments (all from the same file: {file_path}), determine which Reference Comments express the same concern or suggestion as the Generated Comment.

Focus solely on semantic equivalence of the underlying issue — ignore differences in wording, tone, formatting, or exact line numbers. If the Generated Comment addresses the same core problem as a Reference Comment, include that Reference Comment's number.

-Generated Comment-
[L{gen_lines}] {gen_note}

-Reference Comments-
{numbered_refs}

-Output-

For each Reference Comment, briefly state whether it matches and why (one line each).
Then return a JSON array of matched numbers on the LAST line. If none match, return [].

Example:
1. Match — both flag the uninitialized variable risk
2. No match — ref discusses naming, generated discusses null check
3. Match — same concern about missing error handling

[1, 3]

Your answer:
"""
