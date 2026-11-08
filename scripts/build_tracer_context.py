"""Build a token-budgeted review context for one PR checkout with codebase-memory-mcp.

Writes to --out:
  trace.json      what the tool found (changed symbols, callers, callees, line ranges)
  context.md      the packed context text, in rank order
  selection.json  config, token counts, and every included/excluded item with the reason

Only the tokenizer named in the config (or --tokenizer) is loaded, offline. No model runs.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.tracer_context.build import build_context, validate_config  # noqa: E402
from src.tracer_context.cbm import TracerError, trace, tracer_settings  # noqa: E402


def load_config(path: Path) -> dict:
    import yaml

    return validate_config(yaml.safe_load(path.read_text(encoding="utf-8")))


def make_counter(name: str):
    if name == "approx":
        return (lambda text: (len(text.encode("utf-8")) + 3) // 4), "approx (UTF-8 bytes / 4)"
    try:
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(name, local_files_only=True)
    except Exception as error:  # noqa: BLE001 - report any loading failure the same way
        raise SystemExit(
            f"Tokenizer {name!r} is not available offline ({type(error).__name__}).\n"
            f"Download only its tokenizer files, e.g.\n"
            f"  .venv/bin/hf download {name} tokenizer.json tokenizer_config.json\n"
            "or pass --tokenizer with a cached tokenizer, or --tokenizer approx for a rough count.")
    return (lambda text: len(tokenizer.encode(text, add_special_tokens=False))), tokenizer_label(name)


def tokenizer_label(name: str) -> str:
    """Name plus the cached Hugging Face snapshot revision, when there is one."""
    try:
        from huggingface_hub import try_to_load_from_cache

        path = try_to_load_from_cache(name, "tokenizer.json")
    except Exception:  # noqa: BLE001 - a local directory or an odd cache layout
        return name
    if isinstance(path, str) and "/snapshots/" in path:
        return f"{name}@{path.split('/snapshots/')[1].split('/')[0]}"
    return name


def write_json(path: Path, data) -> None:
    path.write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, required=True, help="PR checkout with the PR head checked out")
    parser.add_argument("--base", default="base", help="Base branch or commit of the PR (default: base)")
    parser.add_argument("--out", type=Path, required=True, help="Output folder")
    parser.add_argument("--config", type=Path, default=PROJECT_ROOT / "configs" / "tracer_context.yaml")
    parser.add_argument("--budget", type=int, help="Override packing.token_budget")
    parser.add_argument("--tokenizer", help="Override packing.tokenizer (a cached name/path, or 'approx')")
    parser.add_argument("--skip-trace", action="store_true",
                        help="Reuse --out/trace.json (only selection/ranking/packing changes take effect)")
    parser.add_argument("--keep-index", action="store_true",
                        help="Keep the tool's graph database in --out/index_cache (e.g. for its UI)")
    args = parser.parse_args()

    cfg = load_config(args.config)
    if args.budget is not None:
        if args.budget < 0:
            parser.error("--budget must be >= 0")
        cfg["packing"]["token_budget"] = args.budget
    if args.tokenizer:
        cfg["packing"]["tokenizer"] = args.tokenizer
    count_tokens, tokenizer_name = make_counter(cfg["packing"]["tokenizer"])  # fail before indexing

    repo = args.repo.resolve()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    trace_path = out / "trace.json"
    if args.skip_trace:
        if not trace_path.is_file():
            parser.error(f"--skip-trace: {trace_path} does not exist")
        data = json.loads(trace_path.read_text(encoding="utf-8"))
        head = subprocess_head(repo)
        if data["tracer_settings"] != tracer_settings(cfg["tracer"]):
            parser.error("--skip-trace: config.tracer changed since the trace; run without --skip-trace")
        if data["head"] != head:
            parser.error(f"--skip-trace: {repo} is at {head[:10]}, trace was made at {data['head'][:10]}")
    else:
        binary = (PROJECT_ROOT / cfg["tracer"]["binary"]) if not Path(cfg["tracer"]["binary"]).is_absolute() \
            else Path(cfg["tracer"]["binary"])
        cache = out / "index_cache" if args.keep_index else Path(tempfile.mkdtemp(prefix="cbm-cache-", dir=out))
        print(f"tracing {repo} against {args.base} ...", file=sys.stderr)
        try:
            data = trace(repo, args.base, cfg["tracer"], binary, cache)
        except TracerError as error:
            raise SystemExit(f"error: {error}")
        finally:
            if not args.keep_index:
                shutil.rmtree(cache, ignore_errors=True)
        write_json(trace_path, data)

    text, report = build_context(data, repo, cfg, count_tokens, tokenizer_name)
    (out / "context.md").write_text(text + ("\n" if text else ""), encoding="utf-8")
    write_json(out / "selection.json", report)

    counts = report["counts"]
    print(f"changed files {counts['changed_files']}, changed symbols {counts['seeds']}, "
          f"callers found {counts['callers_found']}, callees found {counts['callees_found']}")
    print(f"included {len(report['included'])} items, {report['context_tokens']}/{report['token_budget']} tokens "
          f"({tokenizer_name}): {counts['included_by_kind']}")
    print(f"excluded: {counts['excluded_by_reason']}")
    for warning in report["warnings"]:
        print(f"warning: {warning}")
    print(f"wrote {out / 'context.md'} and {out / 'selection.json'}")


def subprocess_head(repo: Path) -> str:
    import subprocess

    return subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


if __name__ == "__main__":
    main()
