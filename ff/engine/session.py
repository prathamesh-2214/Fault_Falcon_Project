"""One investigation session: retrieval + generation under one of five cache modes.

Modes (same code, different cache policy):

* ``full``         FullCache on the unpatched model; the whole conversation stays in the cache.
* ``trim``         no persistent cache; each turn re-encodes the last ``total_cache`` tokens
                   of the raw conversation (system prompt / initial query fall out eventually).
* ``streaming``    StreamingCache(4, total - 4): only the 4 sinks are kept.
* ``faultfalcon``  AnchoredStreamingCache: sinks | system | initial_query | checkpoints |
                   lens_baseline | ledger stay in the never-evicted start region; lens +
                   stage + ledger drive retrieval.
* ``ff_no_anchor`` as faultfalcon, but initial_query / checkpoints enter as a normal
                   (evictable) first turn: the ablation that measures the anchor block.

In every mode the initial query and checkpoints are presented at the start of the
conversation; only what happens to them later differs. In ``full`` / ``trim`` /
``streaming`` retrieval is plain top-k for the question (standard RAG chat).

A turn: apply commands -> retrieve -> build the user turn -> generate (manual loop) ->
citation check -> ledger suggestions -> audit row.
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any, Protocol

import torch

from ff import config
from ff.engine import stages as st
from ff.engine.ledger import Ledger
from ff.engine.lens import Lens, apply_command, parse_command
from ff.llm.model import Tokenizer, chat_format, encode, generate, truncate_tokens
from ff.llm.streaming import (
    AnchoredStreamingCache,
    CheckpointLimitError,
    FullCache,
    StreamingCache,
    cache_len,
)
from ff.retrieve.ranker import RetrievalResult

log = logging.getLogger(__name__)
_CITE = re.compile(r"\[((?:evt|chg)_\d+)\]")
FF_MODES = ("faultfalcon", "ff_no_anchor")


class RankerLike(Protocol):
    """What the session needs from the ranker (mocked in tests)."""

    def retrieve(self, lens: Lens, question: str, ledger_text: str = "", anchored_ids: Any = (),
                 budget: int = ..., stage: str | None = None) -> RetrievalResult: ...

    def plain_retrieve(self, question: str, anchored_ids: Any = (), budget: int = ...) -> RetrievalResult: ...

    def compress_id(self, event_id: str) -> str: ...

    def baseline_text(self, service: str, environment: str = "prod") -> str: ...


@dataclass
class SessionFlags:
    """Ablation switches for replay (all on = FaultFalcon)."""

    ledger: bool = True
    anchor: bool = True
    reretrieve_checkpoints: bool = False  # checkpoints re-retrieved every turn instead of anchored
    baseline_pinned: bool = True


@dataclass
class TurnResult:
    """Everything the UI / experiments need about one turn."""

    turn: int
    question: str
    answer: str
    raw_answer: str
    stage: str
    context_ids: list[str]
    anchored_ids: list[str]
    explanations: list[dict[str, Any]]
    unverified: list[str]
    suggestions: list[dict[str, Any]]
    gen_stats: dict[str, Any]
    cache_stats: dict[str, Any]
    rebuild: dict[str, Any] | None
    prompt_tokens: int
    top_change_score: float | None = None
    notes: list[str] = field(default_factory=list)


def check_citations(answer: str, allowed: set[str]) -> tuple[str, list[str]]:
    """Replace ``[evt_x]`` not in ``allowed`` with ``[unverified evt_x]``."""
    bad: list[str] = []

    def sub(m: re.Match[str]) -> str:
        if m.group(1) in allowed:
            return m.group(0)
        bad.append(m.group(1))
        return f"[unverified {m.group(1)}]"

    return _CITE.sub(sub, answer), bad


def cited_ids(answer: str) -> list[str]:
    """Verified ids in citation order."""
    return list(dict.fromkeys(_CITE.findall(answer)))


class Session:
    """A multi-turn investigation bound to one cache mode."""

    def __init__(self, model: torch.nn.Module, tok: Tokenizer, ranker: RankerLike, mode: str = "faultfalcon",
                 store: Any | None = None, session_id: str | None = None, total_cache: int = config.TOTAL_CACHE,
                 max_new_tokens: int = config.MAX_NEW_TOKENS, budget: int = config.RETRIEVAL_BUDGET,
                 flags: SessionFlags | None = None, system_prompt: str = config.SYSTEM_PROMPT,
                 segment_budgets: dict[str, int] | None = None, min_recent: int = config.MIN_RECENT,
                 token_scale: int = 1) -> None:
        """``token_scale`` multiplies every token budget (use 4 with the byte-level tiny tokenizer)."""
        if mode not in config.CACHE_MODES:
            raise ValueError(f"unknown mode {mode!r}; choose from {config.CACHE_MODES}")
        self.model, self.tok, self.ranker, self.mode, self.store = model, tok, ranker, mode, store
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.total_cache = total_cache * token_scale
        self.max_new_tokens, self.budget = max_new_tokens, budget * token_scale
        self.flags = flags or SessionFlags()
        if mode == "ff_no_anchor":
            self.flags = replace(self.flags, anchor=False)
        self.system_prompt = system_prompt
        base = config.SEGMENT_BUDGETS if segment_budgets is None else segment_budgets
        self.budgets = {k: v * (1 if k == "sinks" else token_scale) for k, v in base.items()}
        self.min_recent = min_recent * token_scale
        self.brief = False  # guided Q-A mode: short answers
        self.count: Callable[[str], int] = lambda s: len(encode(self.tok, s))
        self.fmt = chat_format(tok)
        self.close_ids = encode(tok, self.fmt.turn_close)
        # conversation state
        self.initial_query = ""
        self.anchor_ids: list[str] = []
        self.lens: Lens | None = None
        self.ledger = Ledger()
        self.turn_no = 0
        self.history: list[tuple[str, str]] = []
        self.pending_notes: list[str] = []
        self.last_turn_ids: list[int] = []
        self.last_rebuild: dict[str, Any] | None = None
        self.conv_ids: list[int] = []  # trim
        self.past: Any = None  # full / streaming
        self.cache: AnchoredStreamingCache | None = None
        self.policy = self._make_policy()
        self._anchor_prefix = ""

    # ------------------------------------------------------------------ setup
    @property
    def is_ff(self) -> bool:
        return self.mode in FF_MODES

    @property
    def anchored_in_cache(self) -> bool:
        return self.mode == "faultfalcon" and self.flags.anchor

    def _make_policy(self) -> Any:
        if self.mode in ("full", "trim"):
            return FullCache()
        if self.mode == "streaming":
            return StreamingCache(config.SINK_TOKENS, self.total_cache - config.SINK_TOKENS)
        self.cache = AnchoredStreamingCache(self.model, total_size=self.total_cache, min_recent=self.min_recent,
                                            budgets=self.budgets)
        return self.cache

    def _fit(self, text: str, budget: int) -> str:
        return truncate_tokens(self.tok, text, budget)

    def anchor_text(self) -> str:
        """Plain-text anchor block the model can cite."""
        return self._iq_text() + "".join(self._checkpoint_texts())

    def _iq_text(self) -> str:
        return f"Initial question: {self.initial_query}\n"

    def _checkpoint_texts(self) -> list[str]:
        head = "Checkpoints chosen by the engineer:"
        if not self.anchor_ids:
            return [f"{head} none\n"]
        items = [f"{self.ranker.compress_id(e)}\n" for e in self.anchor_ids]
        items[0] = f"{head}\n{items[0]}"
        return items

    def _lens_text(self) -> str:
        assert self.lens is not None
        text = self.lens.render() + "\n"
        if self.flags.baseline_pinned:
            text += self.ranker.baseline_text(self.lens.service, self.lens.environment) + "\n"
        return self._fit(text, self.budgets["lens_baseline"])

    def _ledger_text(self) -> str:
        close = self.fmt.turn_close
        budget = self.budgets["ledger"] - self.count(close) - 2
        text = self.ledger.render(self.count, budget) if self.flags.ledger else "Investigation ledger: (disabled)"
        return self._fit(text, budget) + close

    def segments(self) -> dict[str, Any]:
        """Token ids of each start-region segment (FaultFalcon modes)."""
        sys_ids = encode(self.tok, f"{self.fmt.system_open}{self.system_prompt}\n", first=True)
        segs: dict[str, Any] = {"system": sys_ids, "initial_query": [], "checkpoints": []}
        if self.anchored_in_cache:
            iq = self._fit(self._iq_text(), self.budgets["initial_query"])
            segs["initial_query"] = encode(self.tok, iq)
            segs["checkpoints"] = [encode(self.tok, t) for t in self._checkpoint_texts()]
        segs["lens_baseline"] = encode(self.tok, self._lens_text())
        segs["ledger"] = encode(self.tok, self._ledger_text())
        return segs

    def start(self, initial_query: str, lens: Lens, anchor_ids: list[str] | None = None,
              ledger: Ledger | None = None) -> None:
        """Begin the session: encode the anchor block once (FaultFalcon) or queue it as the
        first user message (other modes)."""
        anchor_ids = list(anchor_ids or [])
        if len(anchor_ids) > config.MAX_CHECKPOINTS:
            raise CheckpointLimitError(f"At most {config.MAX_CHECKPOINTS} checkpoints; drop one.")
        q_budget = self.budgets["initial_query"] - self.count("Initial question: \n")
        self.initial_query = truncate_tokens(self.tok, initial_query.strip(), q_budget)
        self.lens, self.anchor_ids = lens, anchor_ids
        self.ledger = ledger or Ledger()
        self.turn_no, self.history, self.pending_notes = 0, [], []
        if self.is_ff:
            assert self.cache is not None
            self.cache.init_session(self.segments())
            self.last_rebuild = self.cache.last_rebuild
            self._anchor_prefix = "" if self.anchored_in_cache else self.anchor_text()
        else:
            self._anchor_prefix = self.anchor_text()
            self.past, self.conv_ids = None, encode(self.tok, self.fmt.system_block(self.system_prompt), first=True)

    # ------------------------------------------------------------------ retrieval
    def retrieve(self, question: str) -> RetrievalResult:
        assert self.lens is not None
        if not self.is_ff:
            return self.ranker.plain_retrieve(question, budget=self.budget)
        anchored = self.anchor_ids if (self.anchored_in_cache and not self.flags.reretrieve_checkpoints) else []
        ledger_text = self.ledger.render(self.count) if self.flags.ledger else ""
        return self.ranker.retrieve(self.lens, question, ledger_text, anchored_ids=anchored, budget=self.budget,
                                    stage=self.lens.stage)

    def build_user_turn(self, question: str, retrieval: RetrievalResult) -> str:
        parts = []
        if self._anchor_prefix:
            parts.append(self._anchor_prefix.strip())
            self._anchor_prefix = ""
        if self.pending_notes:
            parts.append("\n".join(self.pending_notes))
            self.pending_notes = []
        if self.is_ff:
            assert self.lens is not None
            brief = " Answer in at most 3 short sentences." if self.brief else ""
            parts.append(f"Stage: {self.lens.stage}. {st.instruction(self.lens.stage)}{brief}")
        parts.append("Context:\n" + (retrieval.context_text or "(no records)"))
        parts.append(f"Question: {question}")
        return "\n".join(parts)

    # ------------------------------------------------------------------ one turn
    def turn(self, question: str, on_token: Callable[[str], None] | None = None) -> TurnResult:
        """Apply a command if the message is one, then retrieve + generate."""
        notes: list[str] = []
        cmd = parse_command(question)
        if cmd is not None:
            notes.append(self.apply(cmd.kind, cmd.arg))
            assert self.lens is not None
            question = st.DEFAULT_QUESTIONS[self.lens.stage]
        res = self.generate_turn(question, self.retrieve(question), on_token)
        res.notes = notes + res.notes
        return res

    def generate_turn(self, question: str, retrieval: RetrievalResult,
                      on_token: Callable[[str], None] | None = None) -> TurnResult:
        """Generate the answer for an already-retrieved context (the LangGraph 'generate' node)."""
        assert self.lens is not None, "call start() first"
        self.turn_no += 1
        user_text = self.build_user_turn(question, retrieval)
        user_ids = encode(self.tok, self.fmt.user_block(user_text))
        rebuild = None
        if self.mode == "trim":
            keep = self.total_cache - self.max_new_tokens
            prompt = (self.conv_ids + user_ids)[-keep:]
            g = generate(self.model, self.tok, prompt, None, self.policy, self.max_new_tokens, on_token=on_token)
            self.conv_ids += user_ids + g.token_ids + self.close_ids
            prompt_tokens = len(prompt)
            cache_stats = {"mode": "trim", "sink_tokens": 0, "segments": {}, "rolling_tokens": len(prompt),
                           "total_tokens": len(prompt), "capacity": self.total_cache, "last_rebuild": None}
        elif self.is_ff:
            assert self.cache is not None
            g = generate(self.model, self.tok, user_ids, self.cache.past, self.cache, self.max_new_tokens,
                         on_token=on_token, close_ids=self.close_ids)
            self.cache.past = g.past_key_values
            prompt_tokens = len(user_ids)
            cache_stats = self.cache.stats()
            rebuild, self.last_rebuild = self.last_rebuild, None
        else:
            ids = self.conv_ids + user_ids if self.past is None else user_ids
            g = generate(self.model, self.tok, ids, self.past, self.policy, self.max_new_tokens, on_token=on_token,
                         close_ids=self.close_ids)
            self.past = g.past_key_values
            prompt_tokens = len(ids)
            cache_stats = self.policy.stats(self.past)
        self.last_turn_ids = user_ids + g.token_ids + self.close_ids
        allowed = set(retrieval.ids) | set(self.anchor_ids) | set(self.ledger.cited_ids())
        answer, bad = check_citations(g.text.strip(), allowed)
        suggestions = []
        if self.is_ff and self.flags.ledger:
            suggestions = [s.__dict__ for s in self.ledger.suggestions_from_answer(answer, retrieval.ids)]
        self.history += [("user", question), ("assistant", answer)]
        res = TurnResult(turn=self.turn_no, question=question, answer=answer, raw_answer=g.text, stage=self.lens.stage,
                         context_ids=list(retrieval.ids), anchored_ids=list(self.anchor_ids),
                         explanations=retrieval.explanations, unverified=bad, suggestions=suggestions,
                         gen_stats=g.stats, cache_stats=cache_stats, rebuild=rebuild, prompt_tokens=prompt_tokens,
                         top_change_score=retrieval.top_change_score)
        self._audit(res)
        return res

    def _audit(self, r: TurnResult) -> None:
        if self.store is None:
            return
        self.store.log_turn(self.session_id, r.turn, mode=self.mode, stage=r.stage, question=r.question,
                            context_ids=r.context_ids, anchored_ids=r.anchored_ids, answer=r.answer,
                            ledger=self.ledger.to_list(), cache_stats=r.cache_stats,
                            prefill_ms=r.gen_stats.get("prefill_ms"),
                            decode_ms_per_token=r.gen_stats.get("decode_ms_per_token"), rebuild=r.rebuild)
        self.store.save_ledger(self.session_id, self.ledger.to_list())

    # ------------------------------------------------------------------ engineer actions
    def _rebuild(self, segment: str) -> None:
        """Re-encode ``segment`` onward (FaultFalcon modes); logs tokens and ms."""
        if not self.is_ff:
            return
        assert self.cache is not None
        segs = self.segments()
        self.cache.update_segment(segment, segs[segment], self.last_turn_ids)
        self.last_rebuild = dict(self.cache.last_rebuild or {})
        log.info("rebuild %s: %s tokens in %.0f ms", segment, self.last_rebuild.get("tokens_reencoded"),
                 self.last_rebuild.get("ms", 0.0))

    def _ledger_changed(self, note: str) -> str:
        if self.is_ff and self.flags.ledger:
            self._rebuild("ledger")
        else:
            self.pending_notes.append(f"Engineer note: {note}")
        return note

    def confirm(self, eid: str) -> str:
        self.ledger.confirm(eid)
        return self._ledger_changed(f"[{eid}] confirmed as the root cause.")

    def refute(self, eid: str) -> str:
        self.ledger.refute(eid)
        return self._ledger_changed(f"[{eid}] refuted.")

    def rule_out(self, eid: str) -> str:
        self.ledger.rule_out(eid)
        return self._ledger_changed(f"[{eid}] ruled out.")

    def contributing(self, eid: str) -> str:
        self.ledger.contributing(eid)
        return self._ledger_changed(f"[{eid}] marked as a contributing factor.")

    def drill(self, eid: str) -> str:
        """Confirm ``eid`` as the macro root cause and drill into it (micro: DIFF_ANALYSIS).

        One start-region rebuild covers both the lens (focus) and the ledger, because the
        ledger segment follows the lens segment.
        """
        assert self.lens is not None
        self.ledger.confirm(eid, "root cause (macro)")
        self.lens = replace(self.lens, focus=eid, stage=st.DIFF_ANALYSIS)
        if self.is_ff:
            self._rebuild("lens_baseline")
        else:
            self.pending_notes.append(f"Engineer note: [{eid}] confirmed as the root cause; now find the breaking line.")
        return f"[{eid}] confirmed as the root cause; drilling into its diff."

    def rca(self) -> Any:
        """The RCA report built from this session's ledger and evidence (``ff.engine.rca``)."""
        from ff.engine.rca import build_rca

        return build_rca(self, self.ranker.store)

    def accept_suggestion(self, index: int) -> str:
        line = self.ledger.accept_suggestion(index)
        return self._ledger_changed(line.render())

    def pin(self, eid: str) -> str:
        """Add a checkpoint; refused politely when the anchor block is full."""
        if eid in self.anchor_ids:
            return f"[{eid}] is already a checkpoint."
        if len(self.anchor_ids) >= config.MAX_CHECKPOINTS:
            return f"The anchor block already holds {config.MAX_CHECKPOINTS} checkpoints; unpin one before pinning [{eid}]."
        self.anchor_ids.append(eid)
        if self.anchored_in_cache:
            try:
                self._rebuild("checkpoints")
            except (CheckpointLimitError, ValueError) as e:
                self.anchor_ids.remove(eid)
                return f"Could not pin [{eid}]: {e}"
        else:
            self.pending_notes.append(f"Engineer pinned a checkpoint: {self.ranker.compress_id(eid)}")
        return f"Pinned [{eid}] as a checkpoint."

    def unpin(self, eid: str) -> str:
        if eid not in self.anchor_ids:
            return f"[{eid}] is not a checkpoint."
        self.anchor_ids.remove(eid)
        if self.anchored_in_cache:
            self._rebuild("checkpoints")
        else:
            self.pending_notes.append(f"Engineer unpinned checkpoint [{eid}].")
        return f"Unpinned [{eid}]."

    def set_lens(self, lens: Lens) -> str:
        assert self.lens is not None
        changed = lens.render() != self.lens.render()
        self.lens = lens
        if changed:
            if self.is_ff:
                self._rebuild("lens_baseline")
            else:
                self.pending_notes.append(f"Engineer note: {lens.render()}")
        return lens.render(include_stage=True)

    def set_stage(self, stage: str) -> str:
        assert self.lens is not None
        self.lens = replace(self.lens, stage=stage)
        return f"Stage: {stage}"

    def apply(self, kind: str, arg: str) -> str:
        """Apply a redirect command (lens / focus / stage / pin / unpin)."""
        assert self.lens is not None
        if kind == "pin":
            return self.pin(arg)
        if kind == "unpin":
            return self.unpin(arg)
        if kind == "stage":
            return self.set_stage(arg)
        from ff.engine.lens import Command

        return self.set_lens(apply_command(self.lens, Command(kind, arg)))

    # ------------------------------------------------------------------ views
    def suggest_next_stage(self, last: TurnResult | None = None) -> str:
        assert self.lens is not None
        return st.next_stage(self.lens.stage, last.top_change_score if last else None)

    def cache_stats(self) -> dict[str, Any]:
        if self.is_ff and self.cache is not None:
            return self.cache.stats()
        if self.mode == "trim":
            return {"mode": "trim", "total_tokens": 0, "capacity": self.total_cache, "segments": {}, "sink_tokens": 0,
                    "rolling_tokens": 0, "last_rebuild": None}
        return self.policy.stats(self.past)

    def cache_tokens(self) -> int:
        if self.is_ff and self.cache is not None:
            return cache_len(self.cache.past)
        return cache_len(self.past)

    def state(self) -> dict[str, Any]:
        """JSON-serialisable state (enough to rebuild the start region after a restart)."""
        assert self.lens is not None
        return {"session_id": self.session_id, "mode": self.mode, "initial_query": self.initial_query,
                "anchor_ids": list(self.anchor_ids), "lens": self.lens.to_dict(), "ledger": self.ledger.to_list(),
                "turn": self.turn_no}

    def restore(self, state: dict[str, Any]) -> None:
        """Rebuild from :meth:`state` (the rolling window is lost; the start region is exact)."""
        t0 = time.perf_counter()
        self.start(state["initial_query"], Lens.from_dict(state["lens"]), state.get("anchor_ids", []),
                   Ledger.from_list(state.get("ledger", [])))
        self.turn_no = int(state.get("turn", 0))
        log.warning("session %s rebuilt from saved state in %.0f ms", self.session_id, (time.perf_counter() - t0) * 1000)
