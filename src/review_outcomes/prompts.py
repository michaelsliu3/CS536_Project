"""Reviewer and judge prompts.

Reviewer: OpenAI Codex's built-in `/review` rubric, used verbatim as the system message
(configs/review_prompts/codex_review_rubric.md, from openai/codex at commit
4e4767ffa763ec0d0a19e04cf8ff0163a987297b, codex-rs/prompts/templates/review/rubric.md,
Apache-2.0). Codex's own request tells the reviewer to run `git diff`; a one-call reviewer
cannot, so REVIEW_REQUEST adapts Codex's commit-review request to say the material follows.

Judge: AACR-Bench's file-level semantic judge prompt, copied from alibaba/aacr-bench at
commit ee9d7fef70ef31acea2ff7ae316ce0a9575fe4df, evaluation/judge.py (Apache-2.0).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

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
