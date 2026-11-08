"""Parse a reviewer reply in Codex's review JSON format into AACR-style generated comments."""
from __future__ import annotations

import json
import re

PARSER_VERSION = "codex-json-v1"
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


def parse_review(text: str, known_paths=()) -> dict:
    """{status, findings, invalid_findings, overall, error}.

    status: "ok" (valid JSON, possibly zero findings), "malformed" (no usable JSON object or
    no findings list), or "empty_output". Malformed replies count as empty reviews when
    scored and are reported separately.
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
        findings.append({
            "note": note, "title": title.strip(), "body": body.strip(),
            "path": normalize_path(raw_path, known_paths), "raw_path": raw_path if isinstance(raw_path, str) else None,
            "from_line": start, "to_line": end, "side": "right",
            "priority": _as_int(raw.get("priority")),
            "confidence": raw.get("confidence_score") if isinstance(raw.get("confidence_score"), (int, float)) else None,
        })
    overall = {k: obj.get(k) for k in ("overall_correctness", "overall_explanation", "overall_confidence_score")
               if k in obj}
    return {"status": "ok", "findings": findings, "invalid_findings": invalid, "overall": overall, "error": None}
