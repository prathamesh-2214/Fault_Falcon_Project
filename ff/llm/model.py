"""Model loading, hand-built chat formats and the manual generation loop.

transformers 4.33 has no ``apply_chat_template``, so the chat format is built by hand:
Zephyr for TinyLlama, ChatML for SmolLM / SmolLM2 (``config.MODELS``, chosen by ``FF_MODEL``).
``model.generate()`` is never used with a StreamingLLM cache: :func:`generate` is a manual
token loop (batch size 1, ``attention_mask=None``) that calls ``cache_policy.make_room``
before every chunk and every decoded token.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Protocol

import torch

from ff import config
from ff.llm.streaming import CachePolicy, PastKV, cache_len, forward, pos_shift, prefill

ASSISTANT_CLOSE = "</s>\n"


class Tokenizer(Protocol):
    """The tokenizer surface the engine relies on (HF tokenizers and ByteTokenizer)."""

    bos_token_id: int
    eos_token_id: int

    def encode(self, text: str, add_special_tokens: bool = True) -> list[int]: ...

    def decode(self, ids: Any, skip_special_tokens: bool = False) -> str: ...


# --------------------------------------------------------------------------- loading
@lru_cache(maxsize=2)
def load_model(device: str = "cpu") -> tuple[torch.nn.Module, Tokenizer]:
    """Load the configured model (``FF_MODEL``) once per device: float32 on CPU, float16 on CUDA."""
    from transformers import AutoModelForCausalLM, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(config.MODEL_NAME)
    tok.ff_chat_format = config.MODEL_SPEC.chat_format
    dtype = torch.float16 if device.startswith("cuda") else torch.float32
    model = AutoModelForCausalLM.from_pretrained(config.MODEL_NAME, torch_dtype=dtype)
    model.to(device)
    model.eval()
    return model, tok


@lru_cache(maxsize=1)
def load_tokenizer() -> Tokenizer:
    """The configured model's tokenizer only (no weights)."""
    from transformers import AutoTokenizer

    tok = AutoTokenizer.from_pretrained(config.MODEL_NAME)
    tok.ff_chat_format = config.MODEL_SPEC.chat_format
    return tok


def get_model(tiny: bool | None = None, device: str = "cpu") -> tuple[torch.nn.Module, Tokenizer]:
    """The configured model, or the offline tiny random model when ``tiny`` / ``FF_TINY_MODEL`` is set."""
    if tiny if tiny is not None else config.TINY_MODEL:
        return _tiny_pair()
    return load_model(device)


@lru_cache(maxsize=1)
def _tiny_pair() -> tuple[torch.nn.Module, Tokenizer]:
    from ff.llm.tiny import ByteTokenizer, tiny_llama

    return tiny_llama(), ByteTokenizer()


# --------------------------------------------------------------------------- prompts
@dataclass(frozen=True)
class ChatFormat:
    """Hand-built chat template pieces (transformers 4.33 has no apply_chat_template)."""

    name: str
    system_open: str
    turn_close: str
    user_open: str
    assistant_open: str

    def system_block(self, system: str) -> str:
        return f"{self.system_open}{system}{self.turn_close}"

    def user_block(self, user: str) -> str:
        """A user turn followed by the assistant header the model continues from."""
        return f"{self.user_open}{user}{self.turn_close}{self.assistant_open}"


ZEPHYR = ChatFormat("zephyr", "<|system|>\n", "</s>\n", "<|user|>\n", "<|assistant|>\n")
CHATML = ChatFormat("chatml", "<|im_start|>system\n", "<|im_end|>\n", "<|im_start|>user\n", "<|im_start|>assistant\n")
FORMATS: dict[str, ChatFormat] = {"zephyr": ZEPHYR, "chatml": CHATML}


def chat_format(tok: Any) -> ChatFormat:
    """The format attached to ``tok`` by :func:`load_model` (ByteTokenizer: Zephyr)."""
    return FORMATS[getattr(tok, "ff_chat_format", None) or config.MODEL_SPEC.chat_format]


def format_chat(system: str, turns: Sequence[tuple[str, str]], add_generation_prompt: bool = True,
                fmt: ChatFormat = ZEPHYR) -> str:
    """Build the chat prompt by hand (Zephyr by default; ``fmt=CHATML`` for SmolLM).

    ``turns`` is a list of (role, text) with role ``user`` or ``assistant``. The result
    ends with the assistant header when the last turn is a user turn and
    ``add_generation_prompt`` is true. BOS is NOT included: add it once when encoding the
    start of a conversation (:func:`encode` with ``first=True``).
    """
    out = [fmt.system_block(system)]
    for role, text in turns:
        if role == "user":
            out.append(f"{fmt.user_open}{text}{fmt.turn_close}")
        elif role == "assistant":
            out.append(f"{fmt.assistant_open}{text}{fmt.turn_close}")
        else:
            raise ValueError(f"unknown role {role!r}")
    if add_generation_prompt and turns and turns[-1][0] == "user":
        out.append(fmt.assistant_open)
    return "".join(out)


def encode(tok: Tokenizer, text: str, first: bool = False) -> list[int]:
    """Encode text; BOS only when ``first`` (the very start of a conversation)."""
    return list(tok.encode(text, add_special_tokens=first))


def count_tokens(tok: Tokenizer, text: str) -> int:
    """Token count without special tokens."""
    return len(encode(tok, text))


def truncate_tokens(tok: Tokenizer, text: str, max_tokens: int, note: str = " …[truncated]") -> str:
    """Cut ``text`` to at most ``max_tokens`` tokens, appending ``note`` when cut."""
    ids = encode(tok, text)
    if len(ids) <= max_tokens:
        return text
    keep = max(0, max_tokens - count_tokens(tok, note))
    cut = tok.decode(ids[:keep], skip_special_tokens=True)
    while keep > 0 and count_tokens(tok, cut + note) > max_tokens:
        keep -= 1
        cut = tok.decode(ids[:keep], skip_special_tokens=True)
    return cut + note


# --------------------------------------------------------------------------- generation
@dataclass
class GenerationResult:
    """Output of :func:`generate`."""

    text: str
    token_ids: list[int]
    past_key_values: PastKV | None
    stats: dict[str, Any] = field(default_factory=dict)


def _pick(logits: torch.Tensor, temperature: float, gen: torch.Generator | None) -> int:
    if temperature <= 0:
        return int(torch.argmax(logits).item())
    probs = torch.softmax(logits.float() / temperature, dim=-1)
    return int(torch.multinomial(probs, 1, generator=gen).item())


def generate(model: torch.nn.Module, tok: Tokenizer, input_ids: Sequence[int], past_key_values: PastKV | None,
             cache_policy: CachePolicy, max_new_tokens: int = config.MAX_NEW_TOKENS, temperature: float = 0.0,
             on_token: Callable[[str], None] | None = None, close_ids: Sequence[int] | None = None,
             seed: int = config.SEED) -> GenerationResult:
    """Manual generation loop that respects ``cache_policy``.

    1. ``make_room(past, len(input_ids) + max_new_tokens)`` before prefill.
    2. Prefill in chunks of at most ``recent_size // 2`` (``make_room`` before each).
    3. Decode token by token (greedy if ``temperature == 0``), stopping at ``</s>`` or
       ``max_new_tokens``; ``on_token`` receives each new text delta for streaming.
    4. If ``close_ids`` is given (e.g. the ids of ``"</s>\\n"``), the last sampled token
       and ``close_ids`` are fed so the cache holds the complete assistant turn.
    """
    before = cache_len(past_key_values)
    gen = torch.Generator().manual_seed(seed) if temperature > 0 else None
    with pos_shift(model, cache_policy.needs_pos_shift):
        past = cache_policy.make_room(past_key_values, len(input_ids) + max_new_tokens)
        t0 = time.perf_counter()
        logits, past = prefill(model, list(input_ids), past, cache_policy)
        prefill_ms = (time.perf_counter() - t0) * 1000
        if logits is None:
            raise ValueError("generate() needs at least one input token")
        generated: list[int] = []
        pending: int | None = None
        shown = ""
        t1 = time.perf_counter()
        for step in range(max_new_tokens):
            nxt = _pick(logits[0, -1], temperature, gen)
            if nxt == tok.eos_token_id:
                break
            generated.append(nxt)
            if on_token is not None:
                text = tok.decode(generated, skip_special_tokens=True)
                if len(text) > len(shown):
                    on_token(text[len(shown):])
                    shown = text
            if step == max_new_tokens - 1:
                pending = nxt
                break
            past = cache_policy.make_room(past, 1)
            logits, past = forward(model, [nxt], past)
        decode_s = time.perf_counter() - t1
        if close_ids is not None:
            _, past = prefill(model, ([pending] if pending is not None else []) + list(close_ids), past, cache_policy)
    text = tok.decode(generated, skip_special_tokens=True)
    stats = {
        "prompt_tokens": len(input_ids),
        "new_tokens": len(generated),
        "cache_before": before,
        "cache_after": cache_len(past),
        "prefill_ms": prefill_ms,
        "decode_ms_per_token": decode_s * 1000 / max(1, len(generated)),
    }
    return GenerationResult(text=text, token_ids=generated, past_key_values=past, stats=stats)


def make_chat_fn(model: torch.nn.Module, tok: Tokenizer, max_new_tokens: int = 160) -> Callable[[str, str], str]:
    """A one-shot ``(system, user) -> text`` function (fresh FullCache, greedy) for narration."""
    from ff.llm.streaming import FullCache

    def chat(system: str, user: str) -> str:
        ids = encode(tok, format_chat(system, [("user", user)], fmt=chat_format(tok)), first=True)
        return generate(model, tok, ids, None, FullCache(), max_new_tokens=max_new_tokens).text.strip()

    return chat


@torch.no_grad()
def token_nlls(model: torch.nn.Module, ids: Sequence[int], cache_policy: CachePolicy,
               progress: Callable[[int], None] | None = None) -> tuple[list[float], list[int], list[float]]:
    """Token-by-token NLL with a KV cache (structure of streaming-llm's eval_long_ppl.py).

    Returns (nll per predicted token, cache length after each step, ms per step).
    ``nll[i]`` is the loss of predicting ``ids[i + 1]``.
    """
    nlls: list[float] = []
    lens: list[int] = []
    ms: list[float] = []
    past = None
    with pos_shift(model, cache_policy.needs_pos_shift):
        for i in range(len(ids) - 1):
            t0 = time.perf_counter()
            past = cache_policy.make_room(past, 1)
            logits, past = forward(model, [ids[i]], past)
            logp = torch.log_softmax(logits[0, -1].float(), dim=-1)
            nlls.append(float(-logp[ids[i + 1]].item()))
            lens.append(cache_len(past))
            ms.append((time.perf_counter() - t0) * 1000)
            if progress is not None:
                progress(i)
    return nlls, lens, ms


def perplexity(nlls: Sequence[float]) -> float:
    """exp(mean NLL); ``inf`` for an empty list."""
    return math.exp(sum(nlls) / len(nlls)) if nlls else float("inf")


def distinct_n(text: str, n: int = 2) -> float:
    """Share of distinct word n-grams (a cheap readability / repetition check)."""
    words = text.split()
    grams = [tuple(words[i:i + n]) for i in range(len(words) - n + 1)]
    return len(set(grams)) / len(grams) if grams else 0.0
