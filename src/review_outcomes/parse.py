"""Parse a reviewer reply into AACR-style generated comments.

Two reply formats: Codex's review JSON (`parse_review`), and the tags of the AACR-Bench paper's
prompt for non-agent methods (`parse_tagged_review`). Both return the same shape."""
from __future__ import annotations

import json
import re

PARSER_VERSION = "codex-json-v1"
TAG_PARSER_VERSION = "aacr-tags-v2"  # v2: on_hunk_only
_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)
_FENCE = re.compile(r"^\s*```(?:json)?\s*\n(.*?)\n\s*```\s*$", re.DOTALL)


def _strip_wrappers(text: str) -> str:
    text = _THINK.sub("", text or "")
    if "</think>" in text:  # a reply that opens mid-thought
        text = text.split("</think>", 1)[1]
    match = _FENCE.match(text.strip())
    return match.group(1) if match else text


def extract_review_object(text: str) -> dict | None:
    """The reply's JSON object with a "findings" key, or None.

    Accepts bare JSON, a fenced block, or JSON after some prose, even though the rubric
    asks for bare JSON; anything else is malformed.
    """
    body = _strip_wrappers(text).strip()
    try:
        value = json.loads(body)
        if isinstance(value, dict) and "findings" in value:
            return value
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for start in (i for i, ch in enumerate(body) if ch == "{"):
        try:
            value, _ = decoder.raw_decode(body[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and "findings" in value:
            return value
    return None


def _as_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def normalize_path(path, known_paths=()) -> str:
    """Repository-relative path. Strips `./`, `a/`/`b/` diff prefixes and absolute prefixes,
    using `known_paths` (files from the trace) to recognise the relative part."""
    if not isinstance(path, str) or not path.strip():
        return ""
    known = set(known_paths)
    path = path.strip().replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    if path[:2] in ("a/", "b/") and path[2:] in known:
        path = path[2:]
    path = path.lstrip("/")
    if path in known:
        return path
    suffixes = [k for k in known if path.endswith("/" + k)]
    return max(suffixes, key=len) if suffixes else path


def parse_review(text: str, known_paths=(), path: str | None = None) -> dict:
    """{status, findings, invalid_findings, overall, error}.

    status: "ok" (valid JSON, possibly zero findings), "malformed" (no usable JSON object or
    no findings list), or "empty_output". Malformed replies count as empty reviews when
    scored and are reported separately.

    `path` is the file under review when one call reviews one diff hunk. Every finding is then
    placed in that file, as AACR-Bench's paper does for a per-hunk comment; `reported_path`
    keeps the file the reviewer named.
    """
    if not (text or "").strip():
        return {"status": "empty_output", "findings": [], "invalid_findings": 0, "overall": {}, "error": "empty reply"}
    obj = extract_review_object(text)
    if obj is None or not isinstance(obj.get("findings"), list):
        return {"status": "malformed", "findings": [], "invalid_findings": 0, "overall": {},
                "error": "no JSON object with a findings list"}
    findings, invalid = [], 0
    for raw in obj["findings"]:
        if not isinstance(raw, dict):
            invalid += 1
            continue
        title = raw.get("title") if isinstance(raw.get("title"), str) else ""
        body = raw.get("body") if isinstance(raw.get("body"), str) else ""
        note = "\n\n".join(part.strip() for part in (title, body) if part.strip())
        if not note:
            invalid += 1
            continue
        location = raw.get("code_location") if isinstance(raw.get("code_location"), dict) else {}
        span = location.get("line_range") if isinstance(location.get("line_range"), dict) else {}
        start, end = _as_int(span.get("start")), _as_int(span.get("end"))
        if start is not None and end is None:
            end = start
        raw_path = location.get("absolute_file_path")
        reported = normalize_path(raw_path, known_paths)
        findings.append({
            "note": note, "title": title.strip(), "body": body.strip(),
            "path": reported if path is None else path, "raw_path": raw_path if isinstance(raw_path, str) else None,
            **({} if path is None else {"reported_path": reported}),
            "from_line": start, "to_line": end, "side": "right",
            "priority": _as_int(raw.get("priority")),
            "confidence": raw.get("confidence_score") if isinstance(raw.get("confidence_score"), (int, float)) else None,
        })
    overall = {k: obj.get(k) for k in ("overall_correctness", "overall_explanation", "overall_confidence_score")
               if k in obj}
    return {"status": "ok", "findings": findings, "invalid_findings": invalid, "overall": overall, "error": None}


# ---------- the AACR-Bench paper's tag format ----------

_NOTE = re.compile(r"<note>(.*?)</note>", re.DOTALL | re.IGNORECASE)
_DIFF = re.compile(r"<diff>(.*?)</diff>", re.DOTALL | re.IGNORECASE)
_SIDE = re.compile(r"<side>\s*(left|right)\s*</side>", re.IGNORECASE)
_END = re.compile(r"<end\s*/?>", re.IGNORECASE)


def _diff_lines(diff_text: str) -> list[tuple[str, str, int | None, int | None]]:
    """Every line of every hunk of a diff: (file, text without its +/-/space marker, old line
    number or None, new line number or None)."""
    from src.tracer_context.hunks import split_diff

    lines = []
    for hunk in split_diff(diff_text):
        old, new = hunk.old_start, hunk.new_start
        for text in hunk.body.split("\n")[1:]:
            if text.startswith("\\"):
                continue
            if text.startswith("+"):
                lines.append((hunk.file, text[1:], None, new))
                new += 1
            elif text.startswith("-"):
                lines.append((hunk.file, text[1:], old, None))
                old += 1
            else:
                lines.append((hunk.file, text[1:], old, new))
                old, new = old + 1, new + 1
    return lines


def locate_snippet(diff_text: str, snippet: str, side: str = "right") -> tuple[str, int, int] | None:
    """(file, first line, last line) of a quoted snippet in a diff, on the given side, or None.

    The paper's format has no path or line number: a comment quotes the lines it is about. The
    snippet is matched against the diff's lines as a run, ignoring diff markers and surrounding
    whitespace; lines of the other side are skipped if that is what makes it match. A snippet
    that still does not match is looked up by its first line alone."""
    wanted = [line.strip() for line in snippet.split("\n")]
    wanted = [line[1:].strip() if line[:1] in "+-" else line for line in wanted]
    wanted = [line for line in wanted if line]
    if not wanted:
        return None
    number = 2 if side == "left" else 3
    lines = _diff_lines(diff_text)

    def search(candidates, needle):
        texts = [line[1].strip() for line in candidates]
        for start in range(len(texts) - len(needle) + 1):
            run = candidates[start:start + len(needle)]
            if texts[start:start + len(needle)] == needle and len({line[0] for line in run}) == 1:
                numbers = [line[number] for line in run if line[number] is not None]
                if numbers:
                    return run[0][0], numbers[0], numbers[-1]
        return None

    on_side = [line for line in lines if line[number] is not None]
    return search(lines, wanted) or search(on_side, wanted) or search(on_side, wanted[:1])


def parse_tagged_review(text: str, path: str | None = None, diff: str | None = None,
                        on_hunk_only: bool = False) -> dict:
    """A reply in the AACR-Bench paper's tags, as `parse_review` returns a JSON one.

    Each `<note>` is one finding; the `<diff>` and `<side>` before it (after the previous note)
    belong to it. `path` is the file under review when one call reviews one hunk. `diff` is the
    diff the reviewer was shown: the quoted snippet is looked up in it for the finding's lines
    (and its file, when `path` is not given). A finding whose snippet is not found keeps no line
    numbers (`located` is False).

    `on_hunk_only` is the rule of a per-hunk review: a finding counts only when its quoted
    snippet is found in `diff`, the hunk under review. The others (a quote of other code the
    reviewer was shown, or no quote) are returned under "off_hunk_findings" and are not scored.

    status: "ok" for a reply with a note or the end tag (no note and `<end />` is a review with
    no findings), "malformed" for one with neither, "empty_output" for an empty one."""
    body = _THINK.sub("", text or "")
    if "</think>" in body:
        body = body.split("</think>", 1)[1]
    if not body.strip():
        return {"status": "empty_output", "findings": [], "invalid_findings": 0, "overall": {}, "error": "empty reply"}
    findings, off_hunk, invalid, position = [], [], 0, 0
    for match in _NOTE.finditer(body):
        before, position = body[position:match.start()], match.end()
        note = match.group(1).strip()
        if not note:
            invalid += 1
            continue
        snippets, sides = _DIFF.findall(before), _SIDE.findall(before)
        snippet = snippets[-1].strip("\n") if snippets else None
        side = sides[-1].lower() if sides else "right"
        found = locate_snippet(diff, snippet, side) if diff and snippet else None
        (off_hunk if on_hunk_only and found is None else findings).append({
            "note": note, "title": "", "body": note,
            "path": path if path is not None else (found[0] if found else ""), "raw_path": None,
            "from_line": found[1] if found else None, "to_line": found[2] if found else None, "side": side,
            "priority": None, "confidence": None, "snippet": snippet, "located": found is not None})
    invalid += len(_DIFF.findall(body[position:]))  # a quoted snippet with no comment after it
    ended = bool(_END.search(body))
    extra = {"off_hunk_findings": off_hunk} if on_hunk_only else {}
    if not findings and not off_hunk and not ended:
        return {"status": "malformed", "findings": [], "invalid_findings": invalid, "overall": {},
                "error": "no <note> and no <end />"}
    return {"status": "ok", "findings": findings, "invalid_findings": invalid, "overall": {"ended": ended},
            "error": None, **extra}
