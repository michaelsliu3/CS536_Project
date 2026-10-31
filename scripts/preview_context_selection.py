"""Preview one context policy with a local reviewer tokenizer, without inference."""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.context_selection.baselines import POLICIES, review_messages, select_context
from src.context_selection.prompts import CONTEXT_TYPES


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--record", type=Path, required=True, help="One audited ContextCRBench JSON record")
    parser.add_argument("--tokenizer", required=True, help="Local reviewer tokenizer directory; downloads disabled")
    parser.add_argument("--policy", choices=POLICIES, default="similarity")
    parser.add_argument("--budget", type=int, help="Maximum extra input tokens")
    parser.add_argument("--max-input-tokens", type=int, help="Whole-input cap after reserving generation space")
    parser.add_argument("--fixed-context", nargs="*", choices=CONTEXT_TYPES, default=[])
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--example-id", help="Stable dataset-relative ID for random selection")
    args = parser.parse_args()
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained(args.tokenizer, local_files_only=True)

    def count_tokens(messages):
        return len(tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True))

    example = json.loads(args.record.read_text(encoding="utf-8"))
    selection = select_context(example, args.policy, count_tokens, budget=args.budget,
                               max_input_tokens=args.max_input_tokens,
                               fixed_context=args.fixed_context, seed=args.seed,
                               example_id=args.example_id)
    print(json.dumps({"selection": asdict(selection),
                      "messages": review_messages(example, selection.context_types)}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
