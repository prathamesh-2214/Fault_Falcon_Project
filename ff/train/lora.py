"""LoRA fine-tuning on a laptop CPU, in the app environment (transformers 4.33, no extra packages).

Method: LoRA (Hu et al., 2021). The base weights stay frozen; every attention and MLP projection
(``q/k/v/o_proj``, ``gate/up/down_proj``) gets a trainable low-rank update ``W + (alpha/r) * B @ A``
with ``B`` initialised to zero, so training starts exactly at the base model. For SmolLM2-360M and
r=16 that is ~8.7 M trainable parameters (2.4 %). Why LoRA and not the alternatives:

* full fine-tuning of 362 M parameters with AdamW needs ~6 GB of optimizer state and overfits ~400
  examples quickly; LoRA keeps the base model's general language ability;
* QLoRA needs bitsandbytes + CUDA; this runs on the CPU;
* prompt / prefix tuning would change the anchored start region the StreamingLLM cache relies on.

The loss is on the ANSWER tokens only (the prompt is context, not something to learn to write), and
the training prompt is the exact token sequence the anchored cache holds at runtime
(``ff.train.lora_data``). After training the update is merged into the weights
(``W <- W + scale * B @ A``), so the result is a plain Llama checkpoint that ``FF_MODEL_PATH`` loads
and the pos-shift patch supports; nothing changes at inference time.

    python -m ff.train.lora --data results/lora_train.jsonl --out models/smollm2-360m-ff               # CPU, ~3 h
    python -m ff.train.lora --data results/lora_train.jsonl --out models/smollm2-360m-ff --device cuda # Colab T4, ~15 min

On a GPU the model is trained in float32 as well (360 M parameters fit easily on a T4), so the
merged weights are identical in format to a CPU run.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

import torch
from torch import nn

from ff import config

TARGETS: tuple[str, ...] = ("q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj")


class LoRALinear(nn.Module):
    """``base(x) + scale * dropout(x) @ A^T @ B^T``; ``base`` is frozen."""

    def __init__(self, base: nn.Linear, r: int = 16, alpha: float = 32.0, dropout: float = 0.05) -> None:
        super().__init__()
        self.base, self.r, self.scale = base, r, alpha / r
        self.A = nn.Parameter(torch.empty(r, base.in_features, dtype=base.weight.dtype))
        self.B = nn.Parameter(torch.zeros(base.out_features, r, dtype=base.weight.dtype))
        nn.init.kaiming_uniform_(self.A, a=math.sqrt(5))
        self.drop = nn.Dropout(dropout)
        base.weight.requires_grad_(False)

    @property
    def weight(self) -> torch.Tensor:  # code that reads proj.weight keeps working
        return self.base.weight

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.base(x) + (self.drop(x) @ self.A.t() @ self.B.t()) * self.scale

    def merged(self) -> nn.Linear:
        """The base layer with the update folded into its weight."""
        with torch.no_grad():
            self.base.weight += (self.B @ self.A) * self.scale
        return self.base


def inject(model: nn.Module, targets: Sequence[str] = TARGETS, r: int = 16, alpha: float = 32.0,
           dropout: float = 0.05) -> list[nn.Parameter]:
    """Freeze ``model`` and wrap every target projection; returns the trainable parameters."""
    for p in model.parameters():
        p.requires_grad_(False)
    params: list[nn.Parameter] = []
    for _, parent in list(model.named_modules()):
        for name, child in list(parent.named_children()):
            if name in targets and isinstance(child, nn.Linear):
                lora = LoRALinear(child, r, alpha, dropout)
                setattr(parent, name, lora)
                params += [lora.A, lora.B]
    return params


def merge(model: nn.Module) -> nn.Module:
    """Fold every LoRA update into its base layer (in place); the model is a plain Llama again."""
    for _, parent in list(model.named_modules()):
        for name, child in list(parent.named_children()):
            if isinstance(child, LoRALinear):
                setattr(parent, name, child.merged())
    return model


def adapter_state(model: nn.Module) -> dict[str, torch.Tensor]:
    return {k: v.detach().cpu().clone() for k, v in model.state_dict().items() if k.endswith((".A", ".B"))}


def load_adapter(model: nn.Module, state: dict[str, torch.Tensor]) -> None:
    missing = set(state) - set(model.state_dict())
    if missing:
        raise KeyError(f"adapter keys not in the model: {sorted(missing)[:3]}")
    model.load_state_dict(state, strict=False)


# --------------------------------------------------------------------------- loss
def answer_loss(model: nn.Module, prompt_ids: Sequence[int], answer_ids: Sequence[int]) -> torch.Tensor:
    """Mean cross-entropy of the answer tokens given the prompt (the LM head runs on the answer only)."""
    dev = next(model.parameters()).device
    ids = torch.tensor([list(prompt_ids) + list(answer_ids)], device=dev)
    hidden = model.model(input_ids=ids).last_hidden_state  # type: ignore[attr-defined]
    p = len(prompt_ids)
    logits = model.lm_head(hidden[0, p - 1:-1])  # type: ignore[attr-defined]
    return nn.functional.cross_entropy(logits.float(), torch.tensor(list(answer_ids), device=dev))


def evaluate(model: nn.Module, rows: Iterable[dict[str, Any]]) -> float:
    model.eval()
    losses = []
    with torch.no_grad():
        for r in rows:
            losses.append(float(answer_loss(model, r["prompt_ids"], r["answer_ids"])))
    model.train()
    return sum(losses) / len(losses) if losses else float("nan")


def train(model: nn.Module, rows: list[dict[str, Any]], val: list[dict[str, Any]], epochs: int = 2, lr: float = 2e-4,
          grad_accum: int = 8, r: int = 16, alpha: float = 32.0, seed: int = config.SEED,
          log: Callable[[dict[str, Any]], None] = print, on_epoch: Callable[[int], None] | None = None
          ) -> list[dict[str, Any]]:
    """LoRA-train ``model`` in place on ``rows``; returns the log rows."""
    torch.manual_seed(seed)
    rng = random.Random(seed)
    params = inject(model, r=r, alpha=alpha)
    opt = torch.optim.AdamW(params, lr=lr, weight_decay=0.0)
    total = max(1, math.ceil(len(rows) * epochs / grad_accum))
    warm = max(1, total // 10)
    sched = torch.optim.lr_scheduler.LambdaLR(opt, lambda s: min(1.0, (s + 1) / warm) * max(0.0, (total - s) / total))
    history: list[dict[str, Any]] = []
    entry = {"epoch": 0, "step": 0, "train_loss": float("nan"), "val_loss": evaluate(model, val), "s": 0.0}
    history.append(entry)
    log(entry)
    model.train()
    step, t0 = 0, time.time()
    for epoch in range(1, epochs + 1):
        order = rows[:]
        rng.shuffle(order)
        run: list[float] = []
        for i, row in enumerate(order, 1):
            loss = answer_loss(model, row["prompt_ids"], row["answer_ids"])
            (loss / grad_accum).backward()
            run.append(float(loss))
            if i % grad_accum == 0 or i == len(order):
                torch.nn.utils.clip_grad_norm_(params, 1.0)
                opt.step()
                sched.step()
                opt.zero_grad(set_to_none=True)
                step += 1
                if step % 5 == 0:
                    log({"epoch": epoch, "step": step, "train_loss": sum(run[-grad_accum * 5:]) / len(run[-grad_accum * 5:]),
                         "val_loss": float("nan"), "s": time.time() - t0})
        entry = {"epoch": epoch, "step": step, "train_loss": sum(run) / len(run), "val_loss": evaluate(model, val),
                 "s": time.time() - t0}
        history.append(entry)
        log(entry)
        if on_epoch:
            on_epoch(epoch)
    return history


def split(rows: list[dict[str, Any]], val_frac: float = 0.08, seed: int = config.SEED
          ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validation = whole incident worlds (no prompt shared with training)."""
    worlds = sorted({r["incident"] for r in rows})
    random.Random(seed).shuffle(worlds)
    k = max(1, round(len(worlds) * val_frac)) if len(worlds) > 1 else 0
    held = set(worlds[:k])
    return [r for r in rows if r["incident"] not in held], [r for r in rows if r["incident"] in held]


def save_merged(model: nn.Module, tok: Any, out: Path, meta: dict[str, Any]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    merge(model)
    model.to("cpu")
    model.save_pretrained(out, safe_serialization=False)  # type: ignore[attr-defined]
    tok.save_pretrained(out)
    (out / "faultfalcon_lora.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data", default="results/lora_train.jsonl")
    ap.add_argument("--out", default=f"models/{config.MODEL_KEY}-ff")
    ap.add_argument("--epochs", type=int, default=2)
    ap.add_argument("--lr", type=float, default=2e-4)
    ap.add_argument("--rank", type=int, default=16)
    ap.add_argument("--alpha", type=float, default=32.0)
    ap.add_argument("--grad-accum", type=int, default=8)
    ap.add_argument("--limit", type=int, default=0, help="use only the first N examples (quick runs)")
    ap.add_argument("--threads", type=int, default=0)
    ap.add_argument("--tiny", action="store_true", help="tiny random model (plumbing test)")
    ap.add_argument("--device", default="cpu", help="cpu or cuda")
    args = ap.parse_args(argv)
    if args.threads:
        torch.set_num_threads(args.threads)
    from ff.llm.model import get_model

    rows = [json.loads(line) for line in open(args.data, encoding="utf-8")]
    if args.limit:
        rows = rows[:args.limit]
    fit, val = split(rows)
    model, tok = get_model(tiny=args.tiny)  # float32 on the CPU, then moved (float32) to the device
    model.to(args.device)
    log_path = Path(config.get_paths().results) / "lora_train_log.csv"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(log_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["epoch", "step", "train_loss", "val_loss", "s"])
        w.writeheader()

        def log(e: dict[str, Any]) -> None:
            w.writerow(e)
            f.flush()
            print(f"epoch {e['epoch']} step {e['step']:>3}  train {e['train_loss']:.3f}  val {e['val_loss']:.3f}  "
                  f"[{e['s'] / 60:.1f} min]", flush=True)

        out = Path(args.out)

        def checkpoint(epoch: int) -> None:
            out.mkdir(parents=True, exist_ok=True)
            torch.save(adapter_state(model), out / f"adapter_epoch{epoch}.pt")

        print(f"{len(fit)} training / {len(val)} validation examples; LoRA r={args.rank} on {', '.join(TARGETS)}")
        history = train(model, fit, val, args.epochs, args.lr, args.grad_accum, args.rank, args.alpha, log=log,
                        on_epoch=checkpoint)
    meta = {"base": config.MODEL_SPEC.hf_id, "method": "LoRA", "device": args.device, "rank": args.rank, "alpha": args.alpha,
            "targets": list(TARGETS), "epochs": args.epochs, "lr": args.lr, "examples": len(fit),
            "validation": len(val), "history": history}
    save_merged(model, tok, out, meta)
    print(f"merged model saved to {out}  (use it: FF_MODEL_PATH={out})")


if __name__ == "__main__":
    main()
