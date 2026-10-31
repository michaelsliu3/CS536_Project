"""Label-free context policies for a frozen reviewer; no model dependencies."""
from __future__ import annotations

import hashlib
import itertools
import json
import math
import random
import re
from collections import Counter
from dataclasses import dataclass
from typing import Callable

from src.context_selection.prompts import (
    CONTEXT_TYPES, SYSTEM_PROMPT, build_user_prompt, clean_text, context_chunks,
    FIELD_CHAR_LIMITS,
)

POLICIES = ("diff_only", "all_context", "fixed", "random", "similarity")


def review_messages(example: dict, context_types=CONTEXT_TYPES) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": build_user_prompt(example, context_types=context_types)},
    ]


@dataclass(frozen=True)
class Selection:
    policy: str
    context_types: tuple[str, ...]
    available_context_types: tuple[str, ...]
    input_tokens: int
    diff_only_tokens: int
    extra_context_tokens: int


def _terms(text):
    return Counter(re.findall(r"[a-z_][a-z_0-9]*|[0-9]+", text.casefold()))


def _bm25_scores(diff, chunks):
    """BM25 with k1=1.5, b=0.75, using this example's available chunks as corpus."""
    query = _terms(diff)
    documents = {kind: _terms(text) for kind, text in chunks.items()}
    sizes = {kind: sum(terms.values()) for kind, terms in documents.items()}
    average = sum(sizes.values()) / max(1, len(sizes))
    scores = {}
    for kind, terms in documents.items():
        score = 0.0
        if average:
            for term in query:
                frequency = terms[term]
                support = sum(term in doc for doc in documents.values())
                idf = math.log(1 + (len(documents) - support + 0.5) / (support + 0.5))
                score += idf * frequency * 2.5 / (frequency + 1.5 * (0.25 + 0.75 * sizes[kind] / average))
        scores[kind] = score
    return scores


def select_context(
    example: dict,
    policy: str,
    count_tokens: Callable[[list[dict[str, str]]], int],
    *,
    budget: int | None = None,
    max_input_tokens: int | None = None,
    fixed_context=(),
    seed: int = 42,
    example_id: str | None = None,
) -> Selection:
    """Select before review, without consulting labels or reviewer outcomes.

    count_tokens must tokenize the complete chat with the reviewer's tokenizer,
    including its generation prompt. budget caps extra tokens relative to the
    same diff-only chat; max_input_tokens caps the entire input (reserve output
    space separately). No policy silently truncates the chosen context.
    """
    if policy not in POLICIES:
        raise ValueError(f"Unknown policy: {policy}")
    for name, value in (("budget", budget), ("max_input_tokens", max_input_tokens)):
        if value is not None and (type(value) is not int or value < 0):
            raise ValueError(f"{name} must be a nonnegative integer or None")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    chunks = context_chunks(example)
    available = tuple(chunks)
    costs = {}

    def cost(mask):
        mask = tuple(kind for kind in CONTEXT_TYPES if kind in mask)
        if mask not in costs:
            value = count_tokens(review_messages(example, mask))
            if type(value) is not int or value < 0:
                raise ValueError("count_tokens must return a nonnegative integer")
            costs[mask] = value
        return costs[mask]

    base = cost(())
    if max_input_tokens is not None and base > max_input_tokens:
        raise ValueError("Diff-only prompt exceeds max_input_tokens; apply a documented diff cap first")

    def fits(mask):
        total = cost(mask)
        return ((budget is None or max(0, total - base) <= budget)
                and (max_input_tokens is None or total <= max_input_tokens))

    if policy == "diff_only":
        selected = ()
    elif policy == "all_context":
        selected = available
    elif policy == "fixed":
        requested = set(fixed_context)
        if requested - set(CONTEXT_TYPES):
            raise ValueError("Unknown fixed context type")
        selected = tuple(kind for kind in available if kind in requested)
    elif policy == "random":
        # Four chunks => at most 16 masks. Uniform over feasible subsets, including
        # empty, rather than a length/order-biased random greedy packing.
        feasible = [mask for size in range(len(available) + 1)
                    for mask in itertools.combinations(available, size) if fits(mask)]
        # Use only allowed model inputs if the caller has no stable dataset ID.
        identity = example_id if example_id is not None else review_messages(example)
        key = json.dumps([seed, identity], sort_keys=True, ensure_ascii=False)
        rng = random.Random(int(hashlib.sha256(key.encode()).hexdigest(), 16))
        selected = rng.choice(feasible)
    else:
        diff = clean_text(example["diff_hunk"], FIELD_CHAR_LIMITS["diff_hunk"])
        scores = _bm25_scores(diff, chunks)
        ranked = sorted(available, key=lambda kind: (-scores[kind], CONTEXT_TYPES.index(kind)))
        selected = ()
        for kind in ranked:
            candidate = tuple(k for k in available if k in (*selected, kind))
            if scores[kind] > 0 and fits(candidate):
                selected = candidate
    if not fits(selected):
        raise ValueError(f"{policy} exceeds the token limits; fixed policies are never silently trimmed")
    total = cost(selected)
    return Selection(policy, selected, available, total, base, max(0, total - base))
