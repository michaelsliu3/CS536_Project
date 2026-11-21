#!/usr/bin/env python3
"""A stand-in for the model server, to rehearse a Codex run without a GPU
(scripts/harness/record_codex.py).

    .venv/bin/python scripts/harness/fake_model_server.py --port 8765 --log /tmp/rehearsal/requests
    .venv/bin/python scripts/harness/record_codex.py review --first 1 --keep-repos \\
        --base-url http://127.0.0.1:8765/v1 --harness-dir /tmp/rehearsal/harness
    .venv/bin/python scripts/harness/record_codex.py score --dry-run --harness-dir /tmp/rehearsal/harness

It answers the two calls Codex makes (GET /v1/models, POST /v1/responses) with scripted replies,
so the whole path can be checked: the command line, the sandbox, the saved session files, and the
parsing of the review. No model is involved, so the "review" says nothing about review quality.

A conversation gets two replies. The first is one shell command: the `git diff <commit>` the
request names, or `git status --short`. The second, once the command's output has come back, is
the review: the text of --reply, or else a review in Codex's JSON with one made-up finding on the
first file of the diff the command printed.

With --log, every request body is saved as request_NNN.json: exactly what Codex sent the model
(its instructions, its tool definitions and the conversation so far). Always give --harness-dir
to the recorder in a rehearsal, so the made-up sessions never mix with real ones.
"""
from __future__ import annotations

import argparse
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_GIT_DIFF = re.compile(r"`(git diff [0-9a-f]{7,40})`")
_CWD = re.compile(r"<cwd>(.*?)</cwd>")
_DIFF_FILE = re.compile(r"^diff --git a/(\S+) b/", re.MULTILINE)


def _texts(request: dict) -> list[str]:
    """The text of each message of the conversation, in order."""
    items = request.get("input")
    if isinstance(items, str):
        return [items]
    return ["".join(part.get("text") or "" for part in item.get("content") or () if isinstance(part, dict))
            for item in items or () if isinstance(item, dict) and item.get("type") == "message"]


def scripted_reply(request: dict, reply_text: str | None = None) -> dict:
    """The output item this server answers a Responses API request with."""
    items = request.get("input") if isinstance(request.get("input"), list) else []
    outputs = [item.get("output") for item in items if isinstance(item, dict) and item.get("type") == "function_call_output"]
    tools = {tool.get("name") for tool in request.get("tools") or () if isinstance(tool, dict)}
    texts = _texts(request)
    if not outputs and tools:
        named = _GIT_DIFF.search(texts[-1] if texts else "")
        command = named.group(1) if named else "git status --short"
        if "exec_command" in tools:
            name, arguments = "exec_command", {"cmd": command}
        elif "shell_command" in tools:
            name, arguments = "shell_command", {"command": command}
        else:
            name, arguments = sorted(tools)[0], {"command": command}
        return {"type": "function_call", "id": "fc_" + uuid.uuid4().hex[:12], "call_id": "call_" + uuid.uuid4().hex[:12],
                "name": name, "arguments": json.dumps(arguments), "status": "completed"}
    if reply_text is None:
        shown = "\n".join(output if isinstance(output, str) else json.dumps(output) for output in outputs)
        file, cwd = _DIFF_FILE.search(shown), _CWD.search("\n".join(texts))
        findings = [] if not file else [{
            "title": "[P2] Rehearsal finding, not a real one", "body": "Written by the stand-in server to exercise the parser.",
            "confidence_score": 0.5, "priority": 2,
            "code_location": {"absolute_file_path": f"{cwd.group(1) if cwd else ''}/{file.group(1)}", "line_range": {"start": 1, "end": 1}}}]
        reply_text = json.dumps({"findings": findings, "overall_correctness": "patch is correct",
                                 "overall_explanation": "A rehearsal reply from the stand-in server.", "overall_confidence_score": 0.5})
    return {"type": "message", "id": "msg_" + uuid.uuid4().hex[:12], "role": "assistant", "status": "completed",
            "content": [{"type": "output_text", "text": reply_text, "annotations": []}]}


def make_handler(model: str, log: Path | None, reply_text: str | None):
    counter, lock = [0], threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:  # quiet: the requests are saved, not printed
            pass

        def _json(self, body: dict) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _event(self, name: str, data: dict) -> None:
            self.wfile.write(f"event: {name}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
            self.wfile.flush()

        def do_GET(self) -> None:
            self._json({"object": "list", "data": [{"id": model, "object": "model"}]})

        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            with lock:
                counter[0] += 1
                number = counter[0]
            if log:
                (log / f"request_{number:03d}.json").write_bytes(raw)
            request = json.loads(raw)
            item = scripted_reply(request, reply_text)
            usage = {"input_tokens": len(raw) // 4, "input_tokens_details": {"cached_tokens": 0}, "output_tokens": 50,
                     "output_tokens_details": {"reasoning_tokens": 0}, "total_tokens": len(raw) // 4 + 50}
            response = {"id": "resp_" + uuid.uuid4().hex[:16], "object": "response", "created_at": int(time.time()),
                        "model": model, "status": "in_progress", "output": []}
            done = {**response, "status": "completed", "output": [item], "usage": usage}
            if not request.get("stream"):
                self._json(done)
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            self._event("response.created", {"type": "response.created", "sequence_number": 0, "response": response})
            self._event("response.output_item.done", {"type": "response.output_item.done", "sequence_number": 1, "output_index": 0, "item": item})
            self._event("response.completed", {"type": "response.completed", "sequence_number": 2, "response": done})
            self.close_connection = True

    return Handler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--model", default="Qwen/Qwen3.8-27B", help="the model name to answer as (the one in Codex's settings)")
    parser.add_argument("--log", type=Path, help="a folder to save every request body in")
    parser.add_argument("--reply", type=Path, help="a file with the final reply to give, instead of the made-up review")
    args = parser.parse_args(argv)
    if args.log:
        args.log.mkdir(parents=True, exist_ok=True)
    reply = args.reply.read_text(encoding="utf-8") if args.reply else None
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(args.model, args.log, reply))
    print(f"stand-in model server on http://127.0.0.1:{args.port}/v1 answering as {args.model}; Ctrl-C stops it", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
