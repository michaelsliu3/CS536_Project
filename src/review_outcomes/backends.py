"""Model backends for the reviewer and the judge. All decode greedily.

- OpenAIChat: any OpenAI-compatible /chat/completions server (vLLM `vllm serve`, SGLang,
  ...). This is the path for Qwen3.8-27B on the cloud GPU. Uses only the standard library.
- TransformersChat: a small model in-process (e.g. Qwen/Qwen3.5-0.8B on a laptop) for
  pipeline tests. Its outputs are not reviewer results.
- KeywordOverlapJudge: a deterministic stand-in judge for pipeline tests only.

Each backend is called with a list of chat messages and returns
{text, prompt_tokens, completion_tokens, finish_reason, seconds}; `identity` captures every
setting that changes outputs and goes into the cache key.
"""
from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request


class OpenAIChat:
    def __init__(self, base_url: str, model: str, api_key: str | None = None, max_tokens: int = 4096,
                 enable_thinking: bool | None = False, timeout: float = 3600, model_revision: str | None = None):
        self.base_url, self.url = base_url, base_url.rstrip("/") + "/chat/completions"
        self.model, self.api_key, self.max_tokens, self.timeout = model, api_key, max_tokens, timeout
        self.enable_thinking = enable_thinking
        # No base_url: where the model is served does not change its output, so moving the server to
        # another port or machine keeps the saved cells (the notebook's manifest records the URL).
        self.identity = {"kind": "openai-chat", "model": model, "model_revision": model_revision,
                         "temperature": 0, "max_tokens": max_tokens, "enable_thinking": enable_thinking}

    def __call__(self, messages: list[dict]) -> dict:
        body = {"model": self.model, "messages": messages, "temperature": 0, "max_tokens": self.max_tokens}
        if self.enable_thinking is not None:  # Qwen chat templates; vLLM/SGLang pass this through
            body["chat_template_kwargs"] = {"enable_thinking": self.enable_thinking}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.url, data=json.dumps(body).encode("utf-8"), headers=headers)
        started = time.monotonic()
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                data = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"HTTP {error.code}: {error.read().decode('utf-8', 'replace')[:500]}") from None
        choice = data["choices"][0]
        usage = data.get("usage") or {}
        return {"text": choice["message"].get("content") or "", "finish_reason": choice.get("finish_reason"),
                "prompt_tokens": usage.get("prompt_tokens"), "completion_tokens": usage.get("completion_tokens"),
                "seconds": round(time.monotonic() - started, 2)}


class TransformersChat:
    """Greedy generation with a local Hugging Face model (text-only messages)."""

    def __init__(self, model_id: str, max_new_tokens: int = 2048, enable_thinking: bool | None = False,
                 device: str | None = None, local_files_only: bool = False):
        import torch
        from transformers import AutoTokenizer

        self.device = device or ("cuda" if torch.cuda.is_available()
                                 else "mps" if torch.backends.mps.is_available() else "cpu")
        self.tokenizer = AutoTokenizer.from_pretrained(model_id, local_files_only=local_files_only)
        self.model = self._load_model(model_id, local_files_only).to(self.device).eval()
        self.max_new_tokens, self.enable_thinking = max_new_tokens, enable_thinking
        revision = getattr(self.model.config, "_commit_hash", None) or _cached_revision(model_id)
        self.identity = {"kind": "transformers", "model": model_id, "model_revision": revision,
                         "do_sample": False, "max_new_tokens": max_new_tokens, "enable_thinking": enable_thinking}

    @staticmethod
    def _load_model(model_id, local_files_only):
        import transformers

        errors = []
        for name in ("AutoModelForCausalLM", "AutoModelForImageTextToText"):
            try:
                return getattr(transformers, name).from_pretrained(model_id, dtype="auto",
                                                                   local_files_only=local_files_only)
            except (ValueError, KeyError, AttributeError) as error:  # wrong head for this architecture
                errors.append(f"{name}: {error}")
        raise RuntimeError("could not load a text-generation head: " + " | ".join(errors))

    def __call__(self, messages: list[dict]) -> dict:
        import torch

        kwargs = {} if self.enable_thinking is None else {"enable_thinking": self.enable_thinking}
        prompt = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True, **kwargs)
        inputs = self.tokenizer(prompt, return_tensors="pt").to(self.device)
        started = time.monotonic()
        with torch.no_grad():
            output = self.model.generate(**inputs, do_sample=False, max_new_tokens=self.max_new_tokens,
                                         pad_token_id=self.tokenizer.eos_token_id)
        new_tokens = output[0, inputs["input_ids"].shape[1]:]
        finished = bool(len(new_tokens)) and int(new_tokens[-1]) == self.tokenizer.eos_token_id
        return {"text": self.tokenizer.decode(new_tokens, skip_special_tokens=True),
                "finish_reason": "stop" if finished or len(new_tokens) < self.max_new_tokens else "length",
                "prompt_tokens": int(inputs["input_ids"].shape[1]), "completion_tokens": int(len(new_tokens)),
                "seconds": round(time.monotonic() - started, 2)}


def _cached_revision(model_id: str) -> str | None:
    """The Hugging Face snapshot commit the model was loaded from, if it came from the cache."""
    try:
        from huggingface_hub import try_to_load_from_cache

        path = try_to_load_from_cache(model_id, "config.json")
    except Exception:  # noqa: BLE001 - a local directory or an odd cache layout
        return None
    return path.split("/snapshots/")[1].split("/")[0] if isinstance(path, str) and "/snapshots/" in path else None


def judge_caller(chat) -> callable:
    """Wrap a chat backend as AACR's judge: one user message in, answer text out."""
    def judge(prompt: str) -> str:
        return chat([{"role": "user", "content": prompt}])["text"]
    judge.identity = chat.identity
    return judge


class KeywordOverlapJudge:
    """Pipeline-test judge: matches references sharing enough words. Not a real judge."""

    identity = {"kind": "keyword-overlap-test-judge", "threshold": 0.2}

    def __call__(self, prompt: str) -> str:
        generated = prompt.split("-Generated Comment-", 1)[1].split("-Reference Comments-", 1)[0]
        refs = prompt.split("-Reference Comments-", 1)[1].split("-Output-", 1)[0]
        words = lambda s: set(re.findall(r"[a-z_][a-z0-9_]{2,}", s.lower()))  # noqa: E731
        gen = words(generated)
        matched = []
        for line in refs.strip().splitlines():
            number = re.match(r"(\d+)\. ", line)
            if number:
                ref = words(line)
                if ref and len(gen & ref) / len(gen | ref) >= self.identity["threshold"]:
                    matched.append(int(number.group(1)))
        return json.dumps(matched)
