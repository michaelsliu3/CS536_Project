from __future__ import annotations

import sys
import time
from pathlib import Path

import torch
import transformers

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.models.qwen import QwenCoder


def main() -> None:
    print(f"Python version: {sys.version.split()[0]}")
    print(f"PyTorch version: {torch.__version__}")
    print(f"Transformers version: {transformers.__version__}")

    selected_device = QwenCoder.select_device()
    print(f"Selected device: {selected_device}")

    load_start = time.perf_counter()
    qwen = QwenCoder()
    load_elapsed = time.perf_counter() - load_start

    messages = [
        {"role": "system", "content": "You are a code reviewer."},
        {
            "role": "user",
            "content": (
                "Determine whether this code contains an obvious defect.\n"
                "Return only YES or NO.\n\n"
                "def divide(a, b):\n"
                "    return a / b\n"
            ),
        },
    ]

    gen_start = time.perf_counter()
    response = qwen.generate(messages=messages, max_new_tokens=256, temperature=0.0)
    gen_elapsed = time.perf_counter() - gen_start

    print(f"Model loaded successfully: {qwen.model is not None}")
    print(f"Model loading time (s): {load_elapsed:.2f}")
    print(f"Generation time (s): {gen_elapsed:.2f}")
    print("Model response:")
    print(response)


if __name__ == "__main__":
    main()
