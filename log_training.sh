#!/usr/bin/env bash
# Follow persistent training logs without reconnecting to the original notebook.
set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'HELP'
Usage: ./log_training.sh [RUN_DIRECTORY_OR_LOG_FILE]

Defaults to the active run selected by the training notebook and streams
only new entries continuously until Ctrl+C.

Examples:
  ./log_training.sh
  ./log_training.sh /persistent/checkpoints/my-run
  ./log_training.sh /persistent/my-run/training_log.jsonl

Waits if the log does not exist yet; follows it if recreated.
Press Ctrl+C to stop watching. This does not stop training.
HELP
  exit 0
fi
if (( $# > 1 )); then
  echo "Too many arguments. Use --help for usage." >&2
  exit 2
fi
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if (( $# == 0 )); then
  python_bin="$PROJECT_ROOT/.venv/bin/python"
  if [[ ! -x "$python_bin" ]]; then
    python_bin="$(command -v python3 || true)"
  fi
  if [[ -z "$python_bin" ]]; then
    echo "Python is needed to read RUN_NAME from the notebook; pass a run directory explicitly." >&2
    exit 1
  fi
  run_name="$("$python_bin" - "$PROJECT_ROOT/notebooks/qwen3b_qlora_contextcrbench.ipynb" "$PROJECT_ROOT/checkpoints/active_run.json" <<'PY'
import ast
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    notebook = json.load(handle)
names = []
for cell in notebook["cells"]:
    if cell["cell_type"] != "code":
        continue
    for node in ast.parse("".join(cell["source"])).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "RUN_NAME" for target in node.targets
        ):
            if not isinstance(node.value, ast.Constant) or not isinstance(node.value.value, str):
                raise SystemExit("RUN_NAME must be a string literal in the notebook")
            names.append(node.value.value)
if len(names) != 1:
    raise SystemExit(f"Expected one RUN_NAME assignment in notebook, found {len(names)}")
from pathlib import Path
pointer = Path(sys.argv[2])
if pointer.exists():
    active = json.loads(pointer.read_text())
    if active.get("run_name") == names[0]:
        print(active["directory"])
        raise SystemExit
print(str(Path(sys.argv[1]).parent.parent / "checkpoints" / names[0]))
PY
)"
  log_path="$run_name"
else
  log_path="$1"
fi
if [[ -d "$log_path" || "$log_path" != *.jsonl ]]; then
  log_path="${log_path%/}/training_log.jsonl"
fi
printf 'Watching: %s\nPress Ctrl+C to stop watching (training continues).\n' "$log_path"
exec tail -n 0 -F -- "$log_path"
