from __future__ import annotations

import platform
import sys

import torch


def select_device() -> str:
    if torch.cuda.is_available():
        return "cuda"
    if torch.backends.mps.is_available():
        return "mps"
    return "cpu"


def main() -> None:
    print(f"Platform: {platform.platform()}")
    print(f"Python version: {sys.version.split()[0]}")
    print(f"PyTorch version: {torch.__version__}")

    cuda_available = torch.cuda.is_available()
    print(f"CUDA available: {cuda_available}")
    print(f"CUDA version: {torch.version.cuda}")
    if cuda_available:
        device_index = torch.cuda.current_device()
        props = torch.cuda.get_device_properties(device_index)
        total_gb = props.total_memory / (1024**3)
        print(f"NVIDIA GPU name: {props.name}")
        print(f"CUDA GPU memory (GB): {total_gb:.2f}")
    else:
        print("NVIDIA GPU name: N/A")
        print("CUDA GPU memory (GB): N/A")

    mps_available = torch.backends.mps.is_available()
    print(f"Apple MPS available: {mps_available}")
    print(f"Selected device: {select_device()}")


if __name__ == "__main__":
    main()
