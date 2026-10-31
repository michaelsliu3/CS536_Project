"""Context masks layered over the unchanged shared training/reviewer prompt."""
from src.evaluation.prompts import (
    SYSTEM_PROMPT, FIELD_CHAR_LIMITS, clean_text,
    build_user_prompt as _build_all_context_prompt,
)

CONTEXT_TYPES = ("issue", "pr", "code_before", "code_after")

def context_chunks(example: dict) -> dict[str, str]:
    """Render only allowed input fields; omit unavailable/empty context types.

    These field names are an allowlist, not proof that their text predates review.
    The dataset chronology and embedded answer text still require a human audit.
    """
    fields = {
        "issue": [
            ("Issue title", example.get("issue_title", example.get("issue_tilte", "")), 500),
            ("Issue body", example.get("issue_body"), FIELD_CHAR_LIMITS["issue_body"]),
        ],
        "pr": [
            ("PR title", example.get("pr_title"), 500),
            ("PR body", example.get("pr_body"), FIELD_CHAR_LIMITS["pr_body"]),
        ],
        "code_before": [("Code before", example.get("snippet_before"), FIELD_CHAR_LIMITS["snippet_before"])],
        "code_after": [("Code after", example.get("snippet_after"), FIELD_CHAR_LIMITS["snippet_after"])],
    }
    chunks = {}
    for kind, entries in fields.items():
        sections = []
        for name, value, limit in entries:
            text = clean_text(value, limit)
            if text:
                sections.append(f"{name}:\n{text}")
        if sections:
            chunks[kind] = "\n\n".join(sections)
    return chunks


def build_user_prompt(example: dict, *, context_types=None) -> str:
    """Default to all context; an explicit empty iterable means diff-only."""
    selected = set(CONTEXT_TYPES if context_types is None else context_types)
    if selected - set(CONTEXT_TYPES):
        raise ValueError(f"Unknown context types: {sorted(selected - set(CONTEXT_TYPES))}")
    allowed = {
        "full_name": example["full_name"],
        "lang": example["lang"],
        "diff_hunk": example["diff_hunk"],
        "issue_title": example.get("issue_title", example.get("issue_tilte", "")),
        **{field: example.get(field, "") for field in (
            "issue_body", "pr_title", "pr_body", "snippet_before", "snippet_after")},
    }
    fields = {"issue": ("issue_title", "issue_body"), "pr": ("pr_title", "pr_body"),
              "code_before": ("snippet_before",), "code_after": ("snippet_after",)}
    for kind, names in fields.items():
        if kind not in selected:
            for name in names:
                allowed[name] = ""
    return _build_all_context_prompt(allowed)
