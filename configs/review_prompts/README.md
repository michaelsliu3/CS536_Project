# Review and judge prompts

All three prompts are third-party text used verbatim.

| File | What | Source | License |
|---|---|---|---|
| `codex_review_rubric.md` | The frozen reviewer's system prompt: OpenAI Codex's built-in `/review` rubric, unchanged | [openai/codex](https://github.com/openai/codex) at commit `4e4767ffa763ec0d0a19e04cf8ff0163a987297b`, `codex-rs/prompts/templates/review/rubric.md` | Apache-2.0 (`LICENSE-openai-codex.txt`, `NOTICE-openai-codex.txt`) |
| (in `src/review_outcomes/prompts.py`) | AACR-Bench's file-level semantic judge prompt, unchanged | [alibaba/aacr-bench](https://github.com/alibaba/aacr-bench) at commit `ee9d7fef70ef31acea2ff7ae316ce0a9575fe4df`, `evaluation/judge.py` | Apache-2.0 (`LICENSE-alibaba-aacr-bench.txt`) |

## The AACR-Bench paper's reviewer prompt (the per-hunk test)

| File | What | Source | License |
|---|---|---|---|
| `aacr_nonagent_no_context.md` | The prompt for non-agent methods without code context, unchanged, with its slots `{{ pr_title }}`, `{{ pr_description }}`, `{{ diff_hunk }}` | "AACR-Bench: Evaluating Automatic Code Review with Holistic Repository-Level Context", [arXiv:2601.19494v3](https://arxiv.org/abs/2601.19494), appendix [Figure 16](https://arxiv.org/html/2601.19494v3#A3.F16) | [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) |
| `aacr_nonagent_with_context.md` | The same prompt with its code context section and the slot `{{ contexts }}` | The same paper, appendix [Figure 17](https://arxiv.org/html/2601.19494v3#A3.F17) | CC BY 4.0 |

- **Changes made:** none to the wording. The text was read from the paper's HTML page on November 15, 2026, which shows backticks and apostrophes as typographic quotes; they were put back. The files have the figures' lengths, 1,881 and 1,923 characters plus a final newline.
- **Pinned:** SHA-256 `1dba0ebdec40f6f6d9414e9b6dfc27c6e54116c6d91e6b1731767833b0c4ed97` and `a4583087c014439229343ae8b6f45aa67118a29ed364d81064be8074e505e8ee`. `prompts.py` refuses to run if either file changes. A changed wording is a new file with a new prompt version.
- **Use:** `prompts.load_review_prompt("aacr-hunk")` fills the slots from a cell's input and picks the file by whether the input has any context. The reply is read by `parse.parse_tagged_review`. See `docs/per_hunk_review_test.md`, Section 5.

## The Codex rubric

`codex_review_rubric.md` has SHA-256 `ec60e7f36a1d1c2679ce095c0205ecc56f7dd8fb57707a13ef362072390f219f`.
`src/review_outcomes/prompts.py` refuses to run if the file changes, because every outcome-table cell
depends on it. To adopt a newer Codex rubric, add it as a new file with a new prompt version and
regenerate the table; do not edit this one.

Our only addition is the one-sentence user request in `prompts.py` (`REVIEW_REQUEST`). It adapts
Codex's commit-review request, because Codex's normal request tells the reviewer to run `git diff`
and our one-call reviewer cannot run commands; the PR text, diff and context follow it inline.
