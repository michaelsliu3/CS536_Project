#!/usr/bin/env bash
# Build (or continue) the AACR context dataset in data/aacr.
#   data/build_dataset.sh              build everything not yet built
#   data/build_dataset.sh --dry-run    setup checks and plan only
# Extra arguments go to scripts/dataset/build_dataset.py (see its --help).
# DATASET_ROOT=<folder> overrides the dataset folder.
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="$PROJECT_ROOT/.venv/bin/python"
if [[ ! -x "$PYTHON" ]]; then
  echo "No project environment at $PYTHON; create .venv first (see README.md)." >&2
  exit 1
fi
cd "$PROJECT_ROOT"
exec "$PYTHON" scripts/dataset/build_dataset.py "${DATASET_ROOT:-data/aacr}" "$@"
