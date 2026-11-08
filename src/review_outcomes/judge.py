"""AACR-Bench's file-level semantic coverage metric (port of evaluation/judge.py at
alibaba/aacr-bench ee9d7fef70ef31acea2ff7ae316ce0a9575fe4df, Apache-2.0).

Same grouping by file path, judge prompt, answer parsing (last [...] list, 1-based, in range)
and greedy no-credit-for-repeats rule. One deliberate difference: a failed judge call is
recorded as an error so the cell can be retried, instead of silently counting as no match.
"""
from __future__ import annotations

import json
import re
from typing import Callable

from src.review_outcomes.prompts import judge_prompt

_DETAILS = re.compile(r"<details>.*?</details>", re.DOTALL)
_LIST = re.compile(r"\[[\d,\s]*\]")


def clean_note(text) -> str:
    """AACR's _strip_thinking_tags: drop <details> blocks, squeeze blank lines."""
    if not text:
        return ""
    return re.sub(r"\n{3,}", "\n\n", _DETAILS.sub("", text)).strip()


def normalize_separators(path) -> str:
    """AACR's _normalize_path: only unify separators (paths must otherwise match exactly)."""
    return path.replace("\\/", "/").replace("\\", "/") if path else ""


def _lines(item) -> str:
    start, end = item.get("from_line"), item.get("to_line")
    return f"{start}-{end}" if start and end else "?"


def parse_judge_answer(text: str, n_refs: int) -> list[int]:
    """1-based reference numbers from the last [...] in the judge's answer (AACR's rule)."""
    lists = _LIST.findall(text or "")
    if not lists:
        return []
    numbers = json.loads(lists[-1])
    return [i for i in numbers if isinstance(i, int) and 1 <= i <= n_refs]


def score_review(references: list[dict], generated: list[dict], judge: Callable[[str], str]) -> dict:
    """Score one review against one PR's reference comments.

    references: AACR comment dicts (note, path, from_line, to_line, ...), in dataset order.
    generated: parsed findings in generation order (note, path, from_line, to_line).
    judge: prompt -> judge answer text. Called once per generated comment whose file has
    references; comments on files without references count as misses without a call.
    """
    # AACR's counts: every reference / generated comment with non-empty text, path or not.
    expected = sum(1 for ref in references if ref.get("note"))
    refs_by_path: dict[str, list[tuple[int, str, str]]] = {}
    for index, ref in enumerate(references):
        note, path = clean_note(ref.get("note")), normalize_separators(ref.get("path"))
        if note and path:
            refs_by_path.setdefault(path, []).append((index, note, _lines(ref)))

    records, judge_errors = [], 0
    for order, item in enumerate(generated):
        note, path = clean_note(item.get("note")), normalize_separators(item.get("path"))
        record = {"order": order, "path": path, "from_line": item.get("from_line"), "to_line": item.get("to_line"),
                  "matched_gt_indices": [], "judge_reason": "", "judge_error": None, "judged": False}
        refs = refs_by_path.get(path, []) if note else []
        if refs:
            prompt = judge_prompt(path, _lines(item), note, [n for _, n, _ in refs], [l for _, _, l in refs])
            try:
                answer = judge(prompt)
                record["judge_reason"] = (answer or "")[:500]
                record["matched_gt_indices"] = [refs[number - 1][0] for number in parse_judge_answer(answer, len(refs))]
                record["judged"] = True
            except Exception as error:  # noqa: BLE001 - one failed call must not stop the table
                record["judge_error"] = f"{type(error).__name__}: {error}"[:500]
                judge_errors += 1
        records.append(record)

    covered, matched_generated = set(), 0
    for record in records:  # generation order; references never span files, so order across files is moot
        new = set(record["matched_gt_indices"]) - covered
        record["hit_new_gt"] = bool(new)
        if new:
            matched_generated += 1
            covered |= set(record["matched_gt_indices"])

    n_generated = sum(1 for item in generated if item.get("note"))
    recall = len(covered) / expected if expected else 0.0
    precision = matched_generated / n_generated if n_generated else 0.0
    balanced = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"expected": expected, "generated": n_generated, "matched_generated": matched_generated,
            "covered": len(covered), "covered_indices": sorted(covered),
            "recall": recall, "precision": precision, "balanced": balanced,
            "judge_errors": judge_errors, "records": records}
