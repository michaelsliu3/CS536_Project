"""Shared ContextCRBench review prompt; labels are never included in inputs."""
SYSTEM_PROMPT = """You are a code reviewer. Decide whether the supplied pull request change should be merged. Use only the supplied diff and context. Return exactly one JSON object with one Boolean field named is_merged; do not add markdown or explanation."""

# Character caps prevent one field from monopolizing the model window; tokenization applies the final cap.
FIELD_CHAR_LIMITS = {
    "diff_hunk": 12_000,
    "issue_body": 4_000,
    "pr_body": 4_000,
    "snippet_before": 8_000,
    "snippet_after": 8_000,
}

def clean_text(value, limit: int) -> str:
    if value is None:
        return ""
    return str(value).replace("\x00", "")[:limit].strip()

def build_user_prompt(example: dict) -> str:
    sections = [
        f"Repository: {clean_text(example['full_name'], 500)}",
        f"Language: {clean_text(example['lang'], 100)}",
        f"Diff hunk:\n{clean_text(example['diff_hunk'], FIELD_CHAR_LIMITS['diff_hunk'])}",
    ]
    optional = [
        ("Issue title", clean_text(example.get("issue_title", example.get("issue_tilte", "")), 500)),
        ("Issue body", clean_text(example["issue_body"], FIELD_CHAR_LIMITS["issue_body"])),
        ("PR title", clean_text(example["pr_title"], 500)),
        ("PR body", clean_text(example["pr_body"], FIELD_CHAR_LIMITS["pr_body"])),
        ("Code before", clean_text(example["snippet_before"], FIELD_CHAR_LIMITS["snippet_before"])),
        ("Code after", clean_text(example["snippet_after"], FIELD_CHAR_LIMITS["snippet_after"])),
    ]
    sections.extend(f"{name}:\n{text}" for name, text in optional if text)
    return "\n\n".join(sections)

