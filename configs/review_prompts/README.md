# Review and judge prompts (third-party, used verbatim)

| File | What | Source | License |
|---|---|---|---|
| `codex_review_rubric.md` | The frozen reviewer's system prompt: OpenAI Codex's built-in `/review` rubric, unchanged | [openai/codex](https://github.com/openai/codex) at commit `4e4767ffa763ec0d0a19e04cf8ff0163a987297b`, `codex-rs/prompts/templates/review/rubric.md` | Apache-2.0 (`LICENSE-openai-codex.txt`, `NOTICE-openai-codex.txt`) |
| (in `src/review_outcomes/prompts.py`) | AACR-Bench's file-level semantic judge prompt, unchanged | [alibaba/aacr-bench](https://github.com/alibaba/aacr-bench) at commit `ee9d7fef70ef31acea2ff7ae316ce0a9575fe4df`, `evaluation/judge.py` | Apache-2.0 (`LICENSE-alibaba-aacr-bench.txt`) |

`codex_review_rubric.md` has SHA-256 `ec60e7f36a1d1c2679ce095c0205ecc56f7dd8fb57707a13ef362072390f219f`.
`src/review_outcomes/prompts.py` refuses to run if the file changes, because every outcome-table cell
depends on it. To adopt a newer Codex rubric, add it as a new file with a new prompt version and
regenerate the table; do not edit this one.

Our only addition is the one-sentence user request in `prompts.py` (`REVIEW_REQUEST`). It adapts
Codex's commit-review request, because Codex's normal request tells the reviewer to run `git diff`
and our one-call reviewer cannot run commands; the PR text, diff and context follow it inline.
