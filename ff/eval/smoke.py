"""``make smoke``: prove the pos-shift patch is active on the REAL TinyLlama (CPU is fine).

1. 4,000 tokens of the event stream.
2. Perplexity on tokens 2,000-4,000 with (a) StreamingCache(4, 1020) and (b) WindowCache(1024);
   reference: perplexity of the first 1,000 tokens (no eviction).
3. Expect (a) close to the reference and (b) clearly worse. If (a) is not better than (b)
   the patch is not active: exit code 1.
4. Generate 200 tokens after the 4,000-token context with StreamingCache; distinct-2 > 0.3.

``--tiny`` runs the same code on the random tiny model (CI plumbing check; the numbers
are meaningless there and the verdict is not enforced).
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from ff.config import get_paths
from ff.eval.stream import build_stream_ids
from ff.llm.model import distinct_n, generate, get_model, perplexity, token_nlls
from ff.llm.streaming import FullCache, StreamingCache, WindowCache, pos_shift, prefill


def run(tiny: bool = False, n_tokens: int = 4000, gen_tokens: int = 200) -> dict:
    paths = get_paths().ensure()
    model, tok = get_model(tiny=tiny)
    ids = build_stream_ids(tok, n_tokens, paths)
    half, ref_n = n_tokens // 2, n_tokens // 4
    t0 = time.perf_counter()
    ref, _, _ = token_nlls(model, ids[:ref_n + 1], FullCache())
    out = {"tokens": len(ids), "ppl_first_quarter_full": perplexity(ref)}
    for name, policy in (("streaming_4_1020", StreamingCache(4, 1020)), ("window_1024", WindowCache(1024))):
        nll, lens, _ = token_nlls(model, ids, policy,
                                  progress=lambda i, n=name: print(f"\r{n}: {i}/{len(ids)}", end="", flush=True))
        print()
        out[f"ppl_{name}"] = perplexity(nll[half - 1:])
        out[f"max_cache_{name}"] = max(lens)
    policy = StreamingCache(4, 1020)
    with pos_shift(model, True):
        _, past = prefill(model, ids, None, policy)
    g = generate(model, tok, [ids[-1]], past, policy, max_new_tokens=gen_tokens)
    out.update({"generated": g.text, "distinct2": distinct_n(g.text, 2), "seconds": time.perf_counter() - t0,
                "patch_ok": out["ppl_streaming_4_1020"] < out["ppl_window_1024"], "tiny": tiny})
    (paths.results / ("smoke_tiny.json" if tiny else "smoke.json")).write_text(json.dumps(out, indent=1))
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--tiny", action="store_true")
    ap.add_argument("--tokens", type=int, default=4000)
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    r = run(args.tiny, args.tokens, 40 if args.tiny else 200)
    print(f"perplexity, first {r['tokens'] // 4} tokens (no eviction): {r['ppl_first_quarter_full']:.2f}")
    print(f"perplexity, tokens {r['tokens'] // 2}-{r['tokens']}: streaming(4+1020) {r['ppl_streaming_4_1020']:.2f} | "
          f"window(1024, no sinks) {r['ppl_window_1024']:.2f}")
    print(f"generated after {r['tokens']} tokens (distinct-2 {r['distinct2']:.2f}):\n{r['generated']}")
    if args.tiny:
        return
    if not r["patch_ok"]:
        print("STOP: streaming is not better than window -> the pos-shift patch is not active.")
        sys.exit(1)
    if r["distinct2"] <= 0.3:
        print("WARNING: generated text is repetitive (distinct-2 <= 0.3).")
        sys.exit(1)
    print("OK: attention sinks + pos-shift behave as expected.")


if __name__ == "__main__":
    main()
