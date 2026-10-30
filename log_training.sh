#!/usr/bin/env bash
# Follow persistent training logs without reconnecting to the original notebook.
set -euo pipefail

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  cat <<'HELP'
Usage: ./log_training.sh [RUN_DIRECTORY_OR_LOG_FILE]

Defaults to checkpoints/qwen3b-qlora-split-v1-run1/training_log.jsonl
and streams only new entries continuously until Ctrl+C.

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
log_path="${1:-$PROJECT_ROOT/checkpoints/qwen3b-qlora-split-v1-run1}"
if [[ -d "$log_path" || "$log_path" != *.jsonl ]]; then
  log_path="${log_path%/}/training_log.jsonl"
fi
printf 'Watching: %s\nPress Ctrl+C to stop watching (training continues).\n' "$log_path"
exec tail -n 0 -F -- "$log_path"
