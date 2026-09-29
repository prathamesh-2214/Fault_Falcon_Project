"""Offline stand-ins for tests, CI and dry runs.

* :class:`ByteTokenizer` implements the small tokenizer surface the engine uses
  (``encode``, ``decode``, ``bos_token_id``, ``eos_token_id``) with one token per byte
  and ``<s>`` / ``</s>`` as specials, so the Zephyr prompt format works unchanged.
* :func:`tiny_llama` builds a random 2-layer Llama with grouped-query attention
  (4 query heads, 2 KV heads) so ``repeat_kv`` is exercised.

Nothing here downloads anything.
"""

from __future__ import annotations

import re

import torch
from transformers import LlamaConfig, LlamaForCausalLM

_SPECIAL = re.compile(r"(<s>|</s>)")


class ByteTokenizer:
    """Byte-level tokenizer: ids 0..2 are pad/bos/eos, byte ``b`` is id ``b + 3``."""

    pad_token_id = 0
    bos_token_id = 1
    eos_token_id = 2
    bos_token = "<s>"
    eos_token = "</s>"
    offset = 3
    vocab_size = 256 + 3
    ff_chat_format = "zephyr"

    def encode(self, text: str, add_special_tokens: bool = True) -> list[int]:
        """Encode ``text``; ``<s>``/``</s>`` inside the text map to their special ids."""
        ids: list[int] = [self.bos_token_id] if add_special_tokens else []
        for part in _SPECIAL.split(text):
            if part == "<s>":
                ids.append(self.bos_token_id)
            elif part == "</s>":
                ids.append(self.eos_token_id)
            elif part:
                ids.extend(b + self.offset for b in part.encode("utf-8"))
        return ids

    def decode(self, ids: list[int] | torch.Tensor, skip_special_tokens: bool = False) -> str:
        """Decode ids back to text (invalid UTF-8 is replaced)."""
        if isinstance(ids, torch.Tensor):
            ids = ids.tolist()
        out = bytearray()
        text: list[str] = []
        for i in ids:
            if i >= self.offset:
                out.append(i - self.offset)
                continue
            if out:
                text.append(out.decode("utf-8", errors="replace"))
                out = bytearray()
            if not skip_special_tokens and i in (self.bos_token_id, self.eos_token_id):
                text.append(self.bos_token if i == self.bos_token_id else self.eos_token)
        if out:
            text.append(out.decode("utf-8", errors="replace"))
        return "".join(text)


def tiny_config(**overrides: int | float) -> LlamaConfig:
    """Config of the tiny test model (2 layers, hidden 64, 4 heads, 2 KV heads)."""
    kw: dict = dict(
        vocab_size=ByteTokenizer.vocab_size,
        hidden_size=64,
        intermediate_size=128,
        num_hidden_layers=2,
        num_attention_heads=4,
        num_key_value_heads=2,
        max_position_embeddings=8192,
        rms_norm_eps=1e-6,
        bos_token_id=ByteTokenizer.bos_token_id,
        eos_token_id=ByteTokenizer.eos_token_id,
        pad_token_id=ByteTokenizer.pad_token_id,
    )
    kw.update(overrides)
    return LlamaConfig(**kw)


def tiny_llama(seed: int = 0, **overrides: int | float) -> LlamaForCausalLM:
    """A seeded random tiny Llama in eval mode (float32, CPU)."""
    torch.manual_seed(seed)
    model = LlamaForCausalLM(tiny_config(**overrides))
    model.eval()
    return model
