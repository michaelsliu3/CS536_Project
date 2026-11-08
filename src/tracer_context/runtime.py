"""Helpers shared by the tracer context scripts: config loading, token counting, JSON output."""
from __future__ import annotations

import json
from pathlib import Path

from src.tracer_context.build import merge_config, validate_config


def _load_raw(path: Path, seen: tuple = ()) -> dict:
    """A config file with its `inherit:` chain merged in (the file's own keys win)."""
    import yaml

    path = Path(path).resolve()
    if path in seen:
        raise ValueError(f"config inheritance loops through {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    parent = data.pop("inherit", None)
    return merge_config(_load_raw(path.parent / parent, seen + (path,)), data) if parent else data


def load_config(path: Path) -> dict:
    """Load, resolve `inherit:` (a path relative to the file), and validate a config."""
    return validate_config(_load_raw(path))


def load_level_configs(paths) -> dict[str, dict]:
    """Configs by level name, in the given order; a level's parent must come before it, and
    all must share one `tracer` section (each PR is traced once and every level is packed
    from that trace)."""
    import json as _json

    from src.tracer_context.cbm import tracer_settings

    configs = {}
    for path in paths:
        cfg = load_config(Path(path))
        name, parent = cfg["level"]["name"], cfg["level"]["parent"]
        if name in configs:
            raise SystemExit(f"two configs have level name {name!r}")
        if parent and parent not in configs:
            raise SystemExit(f"level {name!r} needs its parent {parent!r} earlier in the --config list")
        configs[name] = cfg
    tracers = {_json.dumps(tracer_settings(c["tracer"]), sort_keys=True) for c in configs.values()}
    binaries = {c["tracer"]["binary"] for c in configs.values()}
    if len(tracers) > 1 or len(binaries) > 1:
        raise SystemExit("all configs must share the same `tracer` section: each PR is traced once "
                         "and every config is packed from that trace (vary depth with selection.limits)")
    return configs


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


def make_counter(name: str):
    """Return (count_tokens, label). Loads tokenizer files offline only."""
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


def write_json(path: Path, data) -> None:
    Path(path).write_text(json.dumps(data, indent=1, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
