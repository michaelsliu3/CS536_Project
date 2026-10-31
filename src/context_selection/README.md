# Context-selection baselines

This module implements label-free policies for the project's frozen-reviewer
accuracy–cost study. It prepares inputs; it does not train or run Qwen.

| Policy | Selection rule |
| --- | --- |
| `diff_only` | Keep the diff, repository, language, and common system prompt. |
| `all_context` | Add every available allowed context type. |
| `fixed` | Apply a caller-supplied context-type mask; skip missing chunks. |
| `random` | Uniformly sample a feasible subset, including the empty subset. |
| `similarity` | Rank chunks with BM25 against the diff, then greedily add positive-score chunks that fit. |

The four optional types are `issue`, `pr`, `code_before`, and `code_after`.
Issue and PR titles stay with their respective bodies. Empty types are unavailable.
The existing prompt's character caps apply before selection. The prompt wrapper
in this package delegates to the unchanged shared training/reviewer prompt;
its default remains the original all-context format. The shared source file
stays unchanged because training resume includes its hash in the run identity.

## Token budgets

Pass a counter using the **reviewer's tokenizer and chat template**, including
the generation prompt. The module measures each assembled chat rather than
adding independently tokenized chunk lengths. `input_tokens` includes the system
prompt and chat formatting; `diff_only_tokens` measures the same chat without
optional context. `extra_context_tokens` is their difference, floored at zero
for token-boundary effects. The extra-token budget caps this measured increment.

`max_input_tokens` additionally caps the whole input. Reserve output space first:
for a 2,048-token window and 32 output tokens, use 2,016 input tokens. An oversized
diff-only input raises an error. No selected prompt is silently truncated.
`all_context` and `fixed` raise an error if they exceed a supplied limit; silently
dropping chunks would change the baseline being measured.

```python
from src.context_selection.baselines import review_messages, select_context

def count_tokens(messages):
    return len(tokenizer.apply_chat_template(
        messages, tokenize=True, add_generation_prompt=True))

selection = select_context(
    record, "similarity", count_tokens,
    budget=512, max_input_tokens=2016,
)
messages = review_messages(record, selection.context_types)
# Supply these messages to the shared QwenCoder interface on the inference host.
```

Preview one **already audited, locally available** record with an existing local
reviewer tokenizer directory:

```bash
python scripts/preview_context_selection.py \
  --record /path/to/record.json \
  --tokenizer /path/to/local/qwen-tokenizer \
  --policy similarity --budget 512 --max-input-tokens 2016
```

The preview prints selected types, measured token counts, and messages. It loads
only tokenizer files with downloads disabled. It performs no inference or training.

## Evaluation protocol and limits

- Use the saved repository split and identical example IDs, frozen reviewer,
  decoding settings, and tokenizer for every policy. Audit field chronology and
  answer text first: an allowlist cannot detect leakage embedded in issue/PR text.
- Choose the best `fixed` mask and budgets on validation data, then freeze them
  before final testing. A fixed-mask primitive is implemented here; automatic
  validation tuning is not. Up to 16 masks exist when all four types are present.
- Random choices use a seed and stable dataset-relative example ID, independently
  of batch order. Without an ID they use only rendered allowed input fields.
  Average over several seeds. A per-example budget alone does **not** match two
  policies' mean token costs; report actual means and match them on validation.
- BM25 uses k1=1.5 and b=0.75, with the available rendered chunks as a per-example
  corpus. Terms are lowercase identifiers/numbers. Ties use canonical type order;
  zero-overlap chunks are skipped. This is a lexical baseline, not a learned
  selector or an optimal subset search.
- Save selected types and counts with each prediction. Measure selector latency
  (including tokenization) as well as reviewer latency; random selection can count
  up to 16 assembled chats. Preserve strict malformed-output scoring.

No real ContextCRBench metrics, model-quality gains, or token savings are claimed
by these utilities. The learned selector, real-data audit, cached frozen-reviewer
pilot, and accuracy–cost plots remain separate work.

## Local verification and contribution description

These tests need only Python, with no GPU, datasets, or model weights:

```bash
python3 -m unittest discover -s tests -p 'test_context_selection.py' -v
```

A suitable commit title is `feat: add budget-aware context selection baselines`.
An accurate contribution description is: "Implemented label-free frozen-reviewer
context-selection baselines, full-chat token-budget checks, reproducible random
sampling, a local tokenizer preview, and tests for outcome-field exclusion and
budget enforcement." Explain the policies and tests yourself, and acknowledge
LLM coding assistance in the course report as required by the project guidance.
