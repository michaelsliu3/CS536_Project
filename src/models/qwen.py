from __future__ import annotations

import random
from typing import Any

import numpy as np
import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


class QwenCoder:
    """Reusable wrapper for local Qwen inference and later research extensions."""

    def __init__(
        self,
        model_name: str | None = None,
        device: str | None = None,
        quantization: str = "none",
        seed: int = 42,
    ) -> None:
        # Default to the 7B instruct reviewer used by the main study; the 3B model is the pilot fallback.
        self.model_name = model_name or "Qwen/Qwen2.5-Coder-7B-Instruct"
        self.quantization = quantization
        self.device = device or self.select_device()
        self.seed = seed
        self.torch_dtype = self.select_dtype(self.device)
        self.tokenizer = None
        self.model = None

        # Quantized loading is not wired up yet; fail fast instead of silently loading full precision.
        if self.quantization != "none":
            raise ValueError(
                "Only quantization='none' is supported in this initial setup. "
                "Future values can include '4bit' or '8bit'."
            )

        # Seed every RNG before loading so weight placement and generation are reproducible.
        self._set_deterministic_mode(self.seed)
        self._load_model_and_tokenizer()

    @staticmethod
    def select_device() -> str:
        # Prefer CUDA (NVIDIA), then Apple MPS, then CPU so the same code runs on every team machine.
        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
        return "cpu"

    @staticmethod
    def select_dtype(device: str) -> torch.dtype:
        # bf16 avoids fp16 overflow on GPUs that support it (Ampere and newer).
        if device == "cuda":
            return torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        if device == "mps":
            # float16 is the most practical default for large decoder models on MPS.
            return torch.float16
        return torch.float32

    @staticmethod
    def _set_deterministic_mode(seed: int) -> None:
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        if torch.cuda.is_available():
            torch.cuda.manual_seed_all(seed)
            # Trade cuDNN autotuning speed for repeatable kernels.
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False

    def _load_model_and_tokenizer(self) -> None:
        self.tokenizer = AutoTokenizer.from_pretrained(self.model_name)

        # low_cpu_mem_usage streams weights in instead of materializing a full fp32 copy in RAM.
        model_kwargs: dict[str, Any] = {
            "dtype": self.torch_dtype,
            "low_cpu_mem_usage": True,
        }

        self.model = AutoModelForCausalLM.from_pretrained(self.model_name, **model_kwargs)
        self.model.to(self.device)
        # Inference only: disables dropout and other training-time behavior.
        self.model.eval()

    def generate(
        self,
        messages: list[dict[str, str]],
        max_new_tokens: int = 256,
        temperature: float = 0.0,
    ) -> str:
        if self.model is None or self.tokenizer is None:
            raise RuntimeError("Model and tokenizer are not loaded.")

        # Render the chat messages with Qwen's own template and append the assistant-turn marker.
        prompt_text = self.tokenizer.apply_chat_template(
            messages,
            tokenize=False,
            add_generation_prompt=True,
        )
        model_inputs = self.tokenizer([prompt_text], return_tensors="pt").to(self.device)

        # temperature == 0 means greedy decoding, which keeps reviewer comparisons deterministic.
        do_sample = temperature > 0.0
        generation_kwargs: dict[str, Any] = {
            "max_new_tokens": max_new_tokens,
            "do_sample": do_sample,
            # Qwen tokenizers may lack a pad token; fall back to EOS to silence generate() warnings.
            "pad_token_id": self.tokenizer.pad_token_id or self.tokenizer.eos_token_id,
        }
        if do_sample:
            generation_kwargs["temperature"] = temperature

        with torch.inference_mode():
            output_ids = self.model.generate(**model_inputs, **generation_kwargs)

        # Drop the prompt tokens so only the newly generated answer is decoded.
        new_token_ids = output_ids[:, model_inputs["input_ids"].shape[1] :]
        response_text = self.tokenizer.batch_decode(
            new_token_ids, skip_special_tokens=True
        )[0]
        return response_text.strip()
