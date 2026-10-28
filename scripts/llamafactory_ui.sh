#!/usr/bin/env bash
# Local UI for preparing configurations; QLoRA training runs on NVIDIA cloud hardware.
set -euo pipefail
PROJECT_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
LLAMAFACTORY_DIR="$PROJECT_ROOT/.cache/llamafactory"
if [[ ! -x "$LLAMAFACTORY_DIR/venv/bin/llamafactory-cli" ]]; then
  echo "LLaMA-Factory is not installed. Follow the LLaMA-Factory UI section in README.md." >&2
  exit 1
fi
export PATH="$LLAMAFACTORY_DIR/venv/bin:$PATH"
cd "$LLAMAFACTORY_DIR/source"
export GRADIO_SERVER_NAME=127.0.0.1
export GRADIO_SERVER_PORT="${GRADIO_SERVER_PORT:-7860}"
export GRADIO_SHARE=0
export GRADIO_ANALYTICS_ENABLED=False
export HF_HUB_DISABLE_TELEMETRY=1
export TOKENIZERS_PARALLELISM=false
exec "$LLAMAFACTORY_DIR/venv/bin/llamafactory-cli" webui
