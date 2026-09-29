"""Training data for the LoRA fine-tune: the guided investigation, step by step, with gold answers.

Each 'dev' incident (and ``make_variant`` re-draws of it) is walked through the same path an SRE
takes in guided mode (``ff.engine.guide``):

    change timeline (alert service) -> follow the dependency chain -> examine a decoy ("no") ->
    examine the cause ("yes") -> root cause -> breaking line (diff) -> blast radius -> remediation

At every step the prompt is built by the REAL session code (``Session.segments`` for the anchored
start region, ``Session.build_user_turn`` for the turn, the retriever for the context), so the
token ids are exactly what the model sees at runtime. Only the model call is replaced by a gold
answer written from the ground truth (:func:`gold`), in at most 3 short sentences that cite ids
from the context. Held-out incidents are never used (they are the evaluation set).

    python -m ff.train.lora_data --worlds 60 --out results/lora_train.jsonl
"""

from __future__ import annotations

import argparse
import json
import random
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from ff import config
from ff.config import get_paths
from ff.engine import stages as st
from ff.engine.lens import Lens
from ff.engine.rca import action_items, remediation_for
from ff.engine.session import Session, TurnResult, check_citations
from ff.llm.model import encode
from ff.llm.streaming import AnchoredStreamingCache

# --------------------------------------------------------------------------- a model-free session


class DryCache(AnchoredStreamingCache):
    """The anchored cache's bookkeeping (segments, budgets) without running the model."""

    def init_session(self, segments: dict[str, Any]) -> None:  # type: ignore[override]
        segs = {n: [] for n in self.order}
        n_ck = 0
        for name, value in segments.items():
            if name == "checkpoints":
                segs[name], n_ck = self._flatten_checkpoints(value)
            else:
                segs[name] = list(value)
        if "sinks" not in segments:
            segs["sinks"], segs["system"] = segs["system"][:self.sink_size], segs["system"][self.sink_size:]
        self._validate(segs, n_ck)
        self.segments, self.n_checkpoints = segs, n_ck
        self.last_rebuild = {"segment": "all"}

    def update_segment(self, name: str, new_ids: Any, tail_ids: Any = (), past: Any = None) -> None:  # type: ignore[override]
        segs = {n: list(v) for n, v in self.segments.items()}
        n_ck = self.n_checkpoints
        if name == "checkpoints":
            segs[name], n_ck = self._flatten_checkpoints(new_ids)
        else:
            segs[name] = list(new_ids)
        self._validate(segs, n_ck)
        self.segments, self.n_checkpoints = segs, n_ck
        self.last_rebuild = {"segment": name}


class DrySession(Session):
    """A FaultFalcon session whose 'model' writes the gold answer; records (prompt, answer) ids.

    The prompt of a turn is what the anchored cache would hold at that point: the start region,
    then the rolling window (turns since the last rebuild, oldest evicted first), then the turn.
    """

    def __init__(self, tok: Any, ranker: Any, **kw: Any) -> None:
        super().__init__(None, tok, ranker, mode="faultfalcon", **kw)  # type: ignore[arg-type]
        self.window: list[int] = []
        self.examples: list[dict[str, Any]] = []
        self.gold: str = ""

    def _make_policy(self) -> Any:
        self.cache = DryCache(None, total_size=self.total_cache, min_recent=self.min_recent,  # type: ignore[arg-type]
                              budgets=self.budgets)
        return self.cache

    def _rebuild(self, segment: str) -> None:
        super()._rebuild(segment)
        self.window = list(self.last_turn_ids)[-(self.cache.recent_size - 1):]  # type: ignore[union-attr]

    def start(self, *a: Any, **kw: Any) -> None:
        super().start(*a, **kw)
        self.window = []

    def generate_turn(self, question: str, retrieval: Any, on_token: Callable[[str], None] | None = None) -> TurnResult:
        assert self.lens is not None and self.cache is not None
        self.turn_no += 1
        user_ids = encode(self.tok, self.fmt.user_block(self.build_user_turn(question, retrieval)))
        answer_ids = encode(self.tok, self.gold) + self.close_ids
        start = [t for n in self.cache.order for t in self.cache.segments[n]]
        room = self.total_cache - len(start) - len(user_ids) - len(answer_ids)
        window = self.window[-room:] if room > 0 else []
        allowed = set(retrieval.ids) | set(self.anchor_ids) | set(self.ledger.cited_ids())
        _, bad = check_citations(self.gold, allowed)
        self.examples.append({"stage": self.lens.stage, "brief": self.brief, "prompt_ids": start + window + user_ids,
                              "answer_ids": answer_ids, "answer": self.gold, "question": question,
                              "context_ids": list(retrieval.ids), "unverified": bad})
        self.last_turn_ids = user_ids + answer_ids
        self.window = (self.window + self.last_turn_ids)[-(self.cache.recent_size - 1):]
        self.history += [("user", question), ("assistant", self.gold)]
        return TurnResult(turn=self.turn_no, question=question, answer=self.gold, raw_answer=self.gold,
                          stage=self.lens.stage, context_ids=list(retrieval.ids), anchored_ids=list(self.anchor_ids),
                          explanations=[], unverified=bad, suggestions=[], gen_stats={}, cache_stats={}, rebuild=None,
                          prompt_tokens=len(user_ids))


# --------------------------------------------------------------------------- gold answers
def _lag(inc: dict[str, Any], ev: dict[str, Any]) -> str:
    h = (datetime.fromisoformat(inc["incident_start"]) - datetime.fromisoformat(ev["occurred_at"])).total_seconds() / 3600
    return f"{h:.1f} h" if h < 48 else f"{h / 24:.0f} days"


def what_changed(ev: dict[str, Any], cds: list[dict[str, Any]]) -> str:
    """'pool_size 50 -> 5' or the files a change touched."""
    params = [f"{c['param_key']} {c['param_old']} -> {c['param_new']}" for c in cds if c.get("param_key")]
    if params:
        return "; ".join(params[:2])
    files = list(dict.fromkeys(c["file"] for c in cds if c.get("file")))
    if files:
        return ", ".join(f.rsplit("/", 1)[-1] for f in files[:2]) + (f" (+{len(files) - 2} files)" if len(files) > 2 else "")
    return f"{ev.get('version_from') or '?'} -> {ev.get('version_to') or '?'}"


def _label(ev: dict[str, Any]) -> str:
    ver = f" {ev['version_to']}" if ev.get("version_to") and ev["event_type"] in ("DEPLOY", "PATCH", "ROLLBACK") else ""
    return f"{ev['event_type'].lower().replace('_', ' ')} of {ev['service']}{ver}"


def _line(diff: str, sign: str) -> str:
    return next((ln[1:].strip() for ln in (diff or "").splitlines()
                 if ln.startswith(sign) and not ln.startswith(sign * 3) and ln[1:].strip()), "")


@dataclass
class Facts:
    """What a turn's answer should contain (scored by ``ff.train.lora_eval``)."""

    stage: str
    must_cite: list[str] = field(default_factory=list)
    must_name: list[str] = field(default_factory=list)
    verdict: str | None = None  # "yes" / "no" for a suspect, "none" when no change explains it


class Gold:
    """Gold answers for one incident, from its ground truth and the turn's retrieved context."""

    def __init__(self, inc: dict[str, Any], store: Any) -> None:
        self.inc, self.store = inc, store
        self.cause = inc.get("cause_event_id")
        self.template = (inc.get("error_templates") or [""])[0]

    def ev(self, eid: str) -> dict[str, Any]:
        return self.store.get_event(eid) or {}

    def desc(self, eid: str) -> str:
        ev = self.ev(eid)
        return f"{_label(ev)}, {what_changed(ev, self.store.get_change_details([eid]))}"

    def sig(self) -> str:
        return f"`{self.template[:70]}`" if self.template else "the new errors"

    def timeline(self, lens: Lens, ctx: list[str]) -> tuple[str, Facts]:
        alert = self.inc["service"]
        decoys = [d for d in self.inc.get("decoy_ids", []) if d in ctx][:1]
        if self.cause and self.cause in ctx:
            ev = self.ev(self.cause)
            text = (f"[{self.cause}] ({self.desc(self.cause)}) is the change most likely to explain the errors in "
                    f"{alert}: it reached prod {_lag(self.inc, ev)} before the incident.")
            if decoys:
                text += f" [{decoys[0]}] ({self.desc(decoys[0])}) looks unrelated to {self.sig()}."
            return text + f" Next, examine [{self.cause}].", Facts(lens.stage, [self.cause])
        if self.cause:
            path = config.dependency_path(lens.service, self.inc.get("cause_service") or alert)
            hop = path[1] if len(path) > 1 else None
            text = f"None of the changes in view explains {self.sig()} in {alert}."
            if decoys:
                text += f" [{decoys[0]}] ({self.desc(decoys[0])}) does not fit the error signature."
            text += (f" {lens.service} depends on {hop}: check the changes there next." if hop
                     else " Check the runtime evidence next.")
            return text, Facts(lens.stage, [], [hop] if hop else [])
        ruled = f" [{decoys[0]}] ({self.desc(decoys[0])}) does not fit it." if decoys else ""
        return (f"No change in view explains {self.sig()} in {alert}.{ruled} The runtime evidence and the "
                f"dependencies should be checked before blaming a change.", Facts(lens.stage, verdict="none"))

    def suspect(self, eid: str) -> tuple[str, Facts]:
        ev = self.ev(eid)
        if eid == self.cause:
            return (f"Yes: [{eid}] changed {what_changed(ev, self.store.get_change_details([eid]))} in {ev['service']} "
                    f"{_lag(self.inc, ev)} before the incident, and {self.sig()} starts after it. It is the likely "
                    f"root cause; confirm it to open its diff.", Facts(st.SUSPECTS, [eid], verdict="yes"))
        return (f"No: [{eid}] ({self.desc(eid)}) does not match {self.sig()} in {self.inc['service']}. Rule it out.",
                Facts(st.SUSPECTS, [eid], verdict="no"))

    def hypothesis(self, ruled: list[str]) -> tuple[str, Facts]:
        if not self.cause:
            return ("No change explains this incident: none of the changes before it matches the error signature, "
                    "so treat it as an infrastructure or external issue.", Facts(st.HYPOTHESIS, verdict="none"))
        ev = self.ev(self.cause)
        text = f"Root cause: [{self.cause}], the {self.desc(self.cause)}, {_lag(self.inc, ev)} before the incident."
        if ruled:
            text += " Ruled out: " + ", ".join(f"[{r}]" for r in ruled[:2]) + "."
        return text, Facts(st.HYPOTHESIS, [self.cause])

    def diff(self) -> tuple[str, Facts]:
        c = self.inc["culprit"]
        cd = self.store.get_change_detail(c["change_id"]) or {}
        if cd.get("param_key"):
            what = f"it changes `{cd['param_key']}` from {cd['param_old']} to {cd['param_new']}"
        else:
            new, old = _line(c.get("diff", ""), "+"), _line(c.get("diff", ""), "-")
            what = f"`{new[:80]}` replaced `{old[:80]}`" if old and new else f"it adds `{new[:80]}`"
        base = c["file"].rsplit("/", 1)[-1]
        return (f"The breaking line is [{c['change_id']}] {c['file']}:{c['line']} ({c.get('symbol')}): {what}. "
                f"This is what produces {self.sig()}.", Facts(st.DIFF_ANALYSIS, [c["change_id"]], [base]))

    def blast(self) -> tuple[str, Facts]:
        blast = self.inc.get("blast_radius") or [self.inc["service"]]
        src = self.inc.get("cause_service") or self.inc["service"]
        others = [b for b in blast if b != src]
        text = f"Affected: {', '.join(blast)}."
        if others:
            def how(o: str) -> str:
                if config.host_of(o) == src:
                    return f"{o} runs on {src}"
                return f"{o} calls {src}" if src in config.dependencies(o) else f"{o} reaches it through the call chain"
            text += f" The fault starts in {src}; " + "; ".join(how(o) for o in others) + "."
        return text, Facts(st.BLAST_RADIUS, [], blast)

    def remediation(self) -> tuple[str, Facts]:
        ev = self.ev(self.cause)
        c = self.inc["culprit"]
        breaking = self.store.get_change_detail(c["change_id"])
        steps = remediation_for(ev, self.store.get_change_details([self.cause]), breaking)[:2]
        follow = action_items(ev, breaking, self.template)[0]
        text = " ".join(steps) + f" Follow-up: {follow}"
        name = [c["file"].rsplit("/", 1)[-1], ev["service"]]
        return text, Facts(st.REMEDIATION, [], name)


# --------------------------------------------------------------------------- the scripted path
Ask = Callable[[Session, str, str, Facts], Any]


def walk(s: Session, inc: dict[str, Any], ranker: Any, ask: Ask, rng: random.Random | None = None) -> None:
    """Drive ``s`` along the guided path of ``inc``; ``ask(session, question, gold, facts)`` runs each turn
    (the dry session records it; the evaluation generates and scores it)."""
    rng = rng or random.Random(config.SEED)
    g = Gold(inc, ranker.store)
    start = datetime.fromisoformat(inc["incident_start"])
    query = f"{inc['service']} error burst since {start:%H:%M}, what changed?"
    lens = Lens(inc["service"], start, stage=st.CHANGE_TIMELINE)
    anchors = [c["id"] for c in ranker.checkpoint_candidates(lens, query) if c["preselect"]][:3]
    s.start(query, lens, anchors)

    def timeline_turn(question: str) -> list[str]:
        ctx = ranker.retrieve(s.lens, question, s.ledger.render(s.count), anchored_ids=s.anchor_ids,
                              budget=s.budget, stage=s.lens.stage).ids
        text, facts = g.timeline(s.lens, ctx)
        ask(s, question, text, facts)
        return ctx

    ctx = timeline_turn(query)
    target = inc.get("cause_service") or inc["service"]
    for hop in config.dependency_path(inc["service"], target)[1:]:
        if g.cause and g.cause in ctx:
            break
        s.apply("lens", hop)
        ctx = timeline_turn(st.DEFAULT_QUESTIONS[st.CHANGE_TIMELINE])
    ruled: list[str] = []
    decoys = [d for d in inc.get("decoy_ids", []) if d in ctx]
    if decoys and rng.random() < 0.7:  # an engineer examines a wrong candidate first
        d = decoys[0]
        s.set_lens(replace(s.lens, focus=d, stage=st.SUSPECTS))
        ask(s, f"Could [{d}] explain the symptom?", *g.suspect(d))
        s.rule_out(d)
        s.set_lens(replace(s.lens, focus=None))
        ruled.append(d)
    if not g.cause:
        s.set_stage(st.HYPOTHESIS)
        ask(s, st.DEFAULT_QUESTIONS[st.HYPOTHESIS], *g.hypothesis(ruled))
        return
    s.set_lens(replace(s.lens, focus=g.cause, stage=st.SUSPECTS))
    ask(s, f"Could [{g.cause}] explain the symptom?", *g.suspect(g.cause))
    if rng.random() < 0.5:
        s.set_stage(st.HYPOTHESIS)
        ask(s, st.DEFAULT_QUESTIONS[st.HYPOTHESIS], *g.hypothesis(ruled))
    if not inc.get("culprit"):
        return
    s.drill(g.cause)
    ask(s, st.DEFAULT_QUESTIONS[st.DIFF_ANALYSIS], *g.diff())
    s.confirm(inc["culprit"]["change_id"])
    s.set_stage(st.BLAST_RADIUS)
    ask(s, st.DEFAULT_QUESTIONS[st.BLAST_RADIUS], *g.blast())
    s.set_stage(st.REMEDIATION)
    ask(s, st.DEFAULT_QUESTIONS[st.REMEDIATION], *g.remediation())


def record(s: Session, question: str, gold: str, facts: Facts) -> None:
    """``ask`` for the dry session: the gold answer becomes the model's turn."""
    s.gold = gold  # type: ignore[attr-defined]
    s.turn(question)


def dialogue(inc: dict[str, Any], ranker: Any, tok: Any, rng: random.Random, brief_p: float = 0.75,
             token_scale: int = 1) -> list[dict[str, Any]]:
    """The training examples of one incident's guided path."""
    s = DrySession(tok, ranker, token_scale=token_scale)
    s.brief = rng.random() < brief_p
    walk(s, inc, ranker, record, rng)
    return [{**e, "incident": inc["id"], "scenario": inc["scenario_type"]} for e in s.examples if not e["unverified"]]


def build(worlds: int = 60, seed: int = config.SEED, tok: Any = None, token_scale: int = 1) -> list[dict[str, Any]]:
    """Examples from the dev incidents and ``make_variant`` re-draws, until ``worlds`` incident worlds."""
    from ff.eval import common
    from ff.ingest.simulator import load_logs_for_replay, load_world, make_variant
    from ff.llm.model import load_tokenizer
    from ff.retrieve.ranker import Ranker
    from ff.store.vector_store import HashEmbedding, approx_tokens

    paths = get_paths()
    world = load_world(paths)
    logs = load_logs_for_replay(paths)
    baselines = common.load_baselines(paths)
    if tok is None:
        tok = load_tokenizer()
    rng = random.Random(seed)
    dev = [i for i in world.incidents if i["split"] == "dev"]
    out: list[dict[str, Any]] = []
    for k in range(min(worlds, 10**6) if dev else 0):
        inc = dev[k % len(dev)]
        w = world if k < len(dev) else make_variant(world, inc["id"], seed=5000 + k, logs=logs)
        winc = inc if k < len(dev) else w.incidents[0]
        store, vectors = common.build_stores(w, baselines, HashEmbedding())
        out += dialogue(winc, Ranker(store, vectors, count=approx_tokens), tok, rng, token_scale=token_scale)
    return out


def save(rows: list[dict[str, Any]], path: Path, tok: Any) -> None:
    """JSONL: token ids (exact runtime prompt) + decoded text (for other trainers)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({**r, "text": tok.decode(r["prompt_ids"] + r["answer_ids"])}) + "\n")


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--worlds", type=int, default=60, help="incident worlds (10 dev incidents + variants)")
    ap.add_argument("--out", default="results/lora_train.jsonl")
    args = ap.parse_args(argv)
    from ff.llm.model import load_tokenizer

    tok = load_tokenizer()
    rows = build(args.worlds, tok=tok)
    save(rows, Path(args.out), tok)
    by_stage: dict[str, int] = {}
    for r in rows:
        by_stage[r["stage"]] = by_stage.get(r["stage"], 0) + 1
    n = max(len(r["prompt_ids"]) + len(r["answer_ids"]) for r in rows) if rows else 0
    print(f"wrote {len(rows)} examples to {args.out}; by stage {by_stage}; longest {n} tokens")


if __name__ == "__main__":
    main()
