#!/usr/bin/env python3
"""The loop's gate asked of a code model: after a review, would a second review with more code
likely find something the first missed? Runs a local model; calls no reviewer and no judge.

    HF_HUB_OFFLINE=1 .venv/bin/python scripts/analysis/selector_gate_llm.py
    HF_HUB_OFFLINE=1 .venv/bin/python scripts/analysis/selector_gate_llm.py --limit 8 --levels 0    # a quick check

For every reviewed hunk and each level below the widest, the model (by default the 7B
Qwen2.5-Coder that the selector uses, untrained) reads the hunk as the reviewer saw it with no
context, the findings of the saved review at that level, and one question. Its score is
log P(Yes) - log P(No) for the next word, the same reading as the selector's direct question.
scripts/analysis/selector_loop.py takes the scores with --gate-scores and compares them with
the price in place of its trained gate.

The question was written once, before any score was looked at:

    A code reviewer has reviewed the diff hunk below. For that review it was given <what it
    saw>. Its findings are listed after the hunk. The reviewer can be asked to review the hunk
    a second time with more code: <the next level>. A second review costs a full extra request,
    so it should be asked for only when it is likely to pay off. Would the second review likely
    find a real problem in this hunk that the first review missed? Answer with Yes or No only.

The model never sees a reference comment. The hunk text is cut to --hunk-tokens (the diff keeps
at least 60% of the room, as in the selector notebooks) and the findings to --review-tokens.
Scores are cached per model, level and text under the results folder's cache/, so a stopped run
continues.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
import time
from pathlib import Path

import numpy as np

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

LEVELS = ["H01_min", "H02_function", "H03_file", "H04_callees1"]
DATA = PROJECT_ROOT / "data/aacr_hunk_selector_files"
RUN = PROJECT_ROOT / "data/aacr_hunk/outcomes/qwen3.8-27b_aacr-hunk_kept4"
OUT = PROJECT_ROOT / "results/selector185_llm"
MODEL = "Qwen/Qwen2.5-Coder-7B-Instruct"
DIFF_SHARE = 0.6
SYSTEM = "You are an expert code reviewer."
SEEN = ["only the diff and the pull request description", "the diff and the function the hunk changes", "the diff and the whole file"]
MORE = ["the function the hunk changes", "the whole file", "the definitions of what the hunk calls"]
QUESTION = ("A code reviewer has reviewed the diff hunk below. For that review it was given {seen}. Its findings are listed after "
            "the hunk. The reviewer can be asked to review the hunk a second time with more code: {more}. A second review costs a "
            "full extra request, so it should be asked for only when it is likely to pay off. Would the second review likely find a "
            "real problem in this hunk that the first review missed? Answer with Yes or No only.")


# ---------- the text the model reads ----------

def question(level: int) -> str:
    """The gate's question after a review at `level` (0 for no context)."""
    return QUESTION.format(seen=SEEN[level], more=MORE[level])


def split_hunk_text(text: str) -> tuple[str, str]:
    """(pull request part, diff part) of a hunk's no-context text."""
    cut = text.rfind("## Diff")
    return (text[:cut].strip(), text[cut:].strip()) if cut >= 0 else (text.strip(), "")


def cut_hunk_text(tokenizer, text: str, room: int) -> str:
    """The diff, then the pull request text, cut to `room` tokens; the diff keeps at least
    DIFF_SHARE of the room when both are long."""
    pr_part, diff = split_hunk_text(text)
    d_ids = tokenizer(diff, add_special_tokens=False).input_ids
    p_ids = tokenizer(pr_part, add_special_tokens=False).input_ids
    if len(d_ids) + len(p_ids) > room:
        keep = min(len(d_ids), max(int(room * DIFF_SHARE), room - len(p_ids)))
        d_ids, p_ids = d_ids[:keep], p_ids[:room - keep]
    return (tokenizer.decode(d_ids) + "\n\n" + tokenizer.decode(p_ids)).strip()


def findings_text(tokenizer, notes: list[str], room: int) -> str:
    """The review's findings as a numbered list, cut to `room` tokens; a review without findings says so."""
    if not notes:
        return "(The review has no findings.)"
    listed = "\n".join(f"{i}. {' '.join(note.split())}" for i, note in enumerate(notes, 1))
    ids = tokenizer(listed, add_special_tokens=False).input_ids
    return listed if len(ids) <= room else tokenizer.decode(ids[:room]) + " …"


def gate_message(tokenizer, level: int, hunk_text: str, notes: list[str], hunk_tokens: int, review_tokens: int) -> str:
    """What the model is asked for one hunk: the question, the hunk, the findings of the review at `level`."""
    return (question(level) + "\n\n" + cut_hunk_text(tokenizer, hunk_text, hunk_tokens)
            + "\n\n## The first review's findings\n\n" + findings_text(tokenizer, notes, review_tokens))


def yes_minus_no(log_probabilities, yes: list[int], no: list[int]) -> np.ndarray:
    """log P(Yes) - log P(No) from each row of next-token log probabilities; several spellings of a word are summed."""
    rows = np.asarray(log_probabilities, dtype=np.float64)
    def total(ids):
        part = rows[:, ids]
        top = part.max(axis=1, keepdims=True)
        return (top + np.log(np.exp(part - top).sum(axis=1, keepdims=True)))[:, 0]
    return total(yes) - total(no)


# ---------- reading ----------

def load_hunks(data: Path, run: Path, limit: int | None = None) -> list[dict]:
    """Every hunk of the selectors' input file, in its order: its no-context text and the notes of its saved reviews."""
    order = [row["hunk"] for row in json.loads((Path(data) / "hunk_outcomes.json").read_text(encoding="utf-8"))["rows"]]
    texts = {}
    with (Path(data) / "context_levels_slim.jsonl").open(encoding="utf-8") as handle:
        for line in handle:
            for hunk in json.loads(line)["hunks"]:
                texts[hunk["id"]] = hunk["levels"][LEVELS[0]]["text"]
    hunks = []
    for name in order[:limit]:
        notes = [[finding.get("note") or "" for finding in
                  json.loads((Path(run) / "reviews" / name / f"{level}.json").read_text(encoding="utf-8"))["parsed"]["findings"]]
                 for level in LEVELS[:-1]]
        hunks.append({"hunk": name, "text": texts[name], "notes": notes})
    return hunks


# ---------- the model ----------

def score_messages(model_name: str, messages_of, levels: list[int], cache_dir: Path, batch: int, device: str | None = None) -> dict[int, np.ndarray]:
    """The model's score for every message of each level. `messages_of(tokenizer, level)` gives the messages."""
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer

    device = device or ("cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu")
    tokenizer = AutoTokenizer.from_pretrained(model_name)
    if tokenizer.pad_token is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"
    yes = [tokenizer.encode(word, add_special_tokens=False)[0] for word in ("Yes", " Yes")]
    no = [tokenizer.encode(word, add_special_tokens=False)[0] for word in ("No", " No")]
    model, scores = None, {}
    for level in levels:
        messages = messages_of(tokenizer, level)
        digest = hashlib.sha1("\x00".join(messages).encode()).hexdigest()[:10]
        cache = Path(cache_dir) / f"gate_{model_name.replace('/', '_')}_{LEVELS[level]}_{digest}.npy"
        if cache.exists():
            print(f"{LEVELS[level]}: cached, {cache.name}", flush=True)
            scores[level] = np.load(cache)
            continue
        if model is None:
            kind = torch.float32 if device == "cpu" else torch.bfloat16 if device == "cuda" and torch.cuda.is_bf16_supported() else torch.float16
            model = AutoModelForCausalLM.from_pretrained(model_name, dtype=kind).to(device).eval()
        prompts = [tokenizer.apply_chat_template([{"role": "system", "content": SYSTEM}, {"role": "user", "content": message}],
                                                 tokenize=False, add_generation_prompt=True) for message in messages]
        encoded = [tokenizer(prompt, add_special_tokens=False).input_ids for prompt in prompts]
        order = np.argsort([len(ids) for ids in encoded])
        out, started, done = np.zeros(len(encoded)), time.time(), 0
        print(f"{LEVELS[level]}: {len(encoded)} prompts, {sum(map(len, encoded)):,} tokens, longest {max(map(len, encoded)):,}", flush=True)
        with torch.no_grad():
            for begin in range(0, len(order), batch):
                index = order[begin:begin + batch]
                padded = tokenizer.pad({"input_ids": [encoded[i] for i in index]}, return_tensors="pt").to(device)
                last = padded["attention_mask"].sum(1) - 1                          # right padding
                hidden = model.model(**padded).last_hidden_state                    # the head is applied to the last word only
                logits = model.lm_head(hidden[torch.arange(len(index), device=device), last]).float()
                out[index] = yes_minus_no(torch.log_softmax(logits, dim=-1).cpu().numpy(), yes, no)
                done += len(index)
                if (begin // batch) % 50 == 0:
                    print(f"   {done:5d} of {len(encoded)} ({time.time() - started:5.0f} s)", flush=True)
        cache.parent.mkdir(parents=True, exist_ok=True)
        np.save(cache, out)
        scores[level] = out
        print(f"{LEVELS[level]}: done in {time.time() - started:.0f} s; answers Yes for {(out > 0).mean():.1%} of hunks", flush=True)
    return scores


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=DATA, help="the selectors' input folder")
    parser.add_argument("--run", type=Path, default=RUN, help="the run folder that holds reviews/")
    parser.add_argument("--out", type=Path, default=OUT, help="where gate_llm_scores.csv and cache/ go")
    parser.add_argument("--model", default=MODEL)
    parser.add_argument("--levels", type=int, nargs="+", default=[0, 1, 2], help="the levels whose review the gate reads (0: no context)")
    parser.add_argument("--hunk-tokens", type=int, default=1400)
    parser.add_argument("--review-tokens", type=int, default=500)
    parser.add_argument("--batch", type=int, default=4)
    parser.add_argument("--limit", type=int, help="only the first hunks, for a quick check; writes no scores file")
    parser.add_argument("--device", help="cuda, mps or cpu (default: the best there is)")
    args = parser.parse_args(argv)

    hunks = load_hunks(args.data, args.run, args.limit)
    def messages_of(tokenizer, level):
        return [gate_message(tokenizer, level, h["text"], h["notes"][level], args.hunk_tokens, args.review_tokens) for h in hunks]
    cache = args.out / ("cache_check" if args.limit else "cache")
    scores = score_messages(args.model, messages_of, args.levels, cache, args.batch, args.device)
    if args.limit:
        for level, values in scores.items():
            print(LEVELS[level], np.round(values, 2).tolist())
        return 0
    target = args.out / "gate_llm_scores.csv"
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(["hunk", *(f"gate|{LEVELS[level]}" for level in sorted(scores))])
        for i, hunk in enumerate(hunks):
            writer.writerow([hunk["hunk"], *(repr(float(scores[level][i])) for level in sorted(scores))])
    print(f"wrote {target} ({args.model}; levels {sorted(scores)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
