#!/usr/bin/env bash
# Delete the data/aacr dataset build and downloaded repositories, so the next build starts over.
#   data/clean_dataset.sh              list what goes, then ask before deleting
#   data/clean_dataset.sh --dry-run    only list
# Extra arguments go to scripts/dataset/clean_dataset.py (--logs, --all, --yes; see its --help).
# DATASET_ROOT=<folder> overrides the dataset folder.
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "No project environment at $PYTHON; create .venv first (see README.md)." >&2
  exit 1
fi
cd "$PROJECT_ROOT"
exec "$PYTHON" scripts/dataset/clean_dataset.py "${DATASET_ROOT:-data/aacr}" "$@"
