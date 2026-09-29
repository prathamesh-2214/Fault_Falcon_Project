"""LangGraph orchestration of the investigation flow — WITHOUT LangChain.

Every node is a plain function calling our own code (ranker, Session.generate_turn with
the manual StreamingLLM loop, AnchoredStreamingCache). The graph adds pauses for the
engineer (``interrupt``), saved state (a checkpointer) and the stage loop.

Naming: LangGraph's *checkpointer* saves graph state; the engineer's *checkpoints* are
called ``anchor_ids`` in code.

Flow::

    START -> scope -> pick_anchors -> retrieve -> generate -> engineer -> apply_action
    apply_action --(close / stage CLOSE)--> close -> END
    apply_action --(question, stage jump, lens)--> retrieve
    apply_action --(ledger / pin click only)--> engineer   (no pointless re-generation)

Rules: nodes that call ``interrupt()`` have no side effects before it (a resumed node
re-runs from its first line); the KV cache stays out of the state (cache_registry).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ff import config
from ff.engine import cache_registry
from ff.engine import stages as st
from ff.engine.lens import Lens, parse_initial_query
from ff.engine.session import Session
from ff.retrieve.ranker import RetrievalResult


class FFState(TypedDict, total=False):
    """JSON-serialisable graph state (no tensors)."""

    thread_id: str
    initial_query: str
    anchor_ids: list[str]
    lens: dict[str, Any]
    stage: str
    ledger: list[dict[str, Any]]
    question: str
    context_ids: list[str]
    context_text: str
    explanations: list[dict[str, Any]]
    answer: str
    unverified: list[str]
    suggested_checks: list[str]
    action: dict[str, Any]
    route: str
    turn: int
    cache_mode: str
    notes: list[str]
    cache_stats: dict[str, Any]
    gen_stats: dict[str, Any]
    postmortem: str


@dataclass
class GraphDeps:
    """What the nodes need. ``session_factory(mode, thread_id) -> Session``."""

    ranker: Any
    session_factory: Callable[[str, str], Session]
    store: Any | None = None
    on_token: Callable[[str], None] | None = None


def _stream_writer() -> Callable[[Any], None]:
    try:
        from langgraph.config import get_stream_writer

        return get_stream_writer()
    except Exception:  # outside a streaming run
        return lambda _chunk: None


def build_graph(deps: GraphDeps, checkpointer: Any | None = None):
    """Compile the investigation graph."""

    def session_for(state: FFState) -> Session:
        return cache_registry.get_or_rebuild(dict(state), deps.session_factory)

    # ---------------------------------------------------------------- nodes
    def scope(state: FFState) -> dict[str, Any]:
        given = state.get("lens") or {}
        start = datetime.fromisoformat(given["incident_start"]) if given.get("incident_start") else None
        lens = parse_initial_query(state["initial_query"], given.get("service"), start)
        return {"lens": lens.to_dict(), "stage": st.SCOPE, "turn": 0, "ledger": [], "anchor_ids": [], "notes": []}

    def pick_anchors(state: FFState) -> dict[str, Any]:
        lens = Lens.from_dict(state["lens"])
        cands = deps.ranker.checkpoint_candidates(lens, state["initial_query"])  # safe to repeat
        chosen = interrupt({"type": "pick_anchors", "candidates": cands})
        # ---- side effects only after the interrupt
        chosen = list(dict.fromkeys(chosen or []))[:config.MAX_CHECKPOINTS]
        lens = replace(lens, stage=st.CHANGE_TIMELINE)
        s = cache_registry.create(state["thread_id"], deps.session_factory, state.get("cache_mode") or "faultfalcon")
        s.start(state["initial_query"], lens, chosen)
        return {"anchor_ids": chosen, "stage": st.CHANGE_TIMELINE, "question": state["initial_query"],
                "lens": lens.to_dict()}

    def retrieve(state: FFState) -> dict[str, Any]:
        s = session_for(state)
        r = s.retrieve(state["question"])
        return {"context_ids": r.ids, "context_text": r.context_text, "explanations": r.explanations}

    def generate(state: FFState) -> dict[str, Any]:
        s = session_for(state)
        writer = _stream_writer()

        def on_token(t: str) -> None:
            writer({"token": t})
            if deps.on_token:
                deps.on_token(t)

        r = RetrievalResult(state.get("context_text", ""), list(state.get("context_ids", [])),
                            list(state.get("explanations", [])))
        res = s.generate_turn(state["question"], r, on_token)
        nxt = s.suggest_next_stage(res)
        checks = [f"stage {nxt.lower()}"] + [f"rule_out {i}" for i in res.context_ids[:2]]
        return {"answer": res.answer, "unverified": res.unverified, "turn": s.turn_no, "suggested_checks": checks,
                "cache_stats": res.cache_stats, "gen_stats": res.gen_stats, "ledger": s.ledger.to_list()}

    def engineer(state: FFState) -> dict[str, Any]:
        action = interrupt({"type": "answer", "answer": state.get("answer", ""), "stage": state.get("stage"),
                            "cited_ids": state.get("context_ids", []),
                            "suggested_checks": state.get("suggested_checks", [])})
        return {"action": action}

    def apply_action(state: FFState) -> dict[str, Any]:
        s = session_for(state)
        a = state.get("action") or {}
        kind = a.get("type", "ask")
        route, note = "engineer", ""
        if kind in ("confirm", "refute", "rule_out", "contributing"):
            note = getattr(s, kind)(a["id"])
        elif kind == "drill":  # confirm the macro root cause and move to the micro stage
            note = s.drill(a["id"])
            route = "retrieve"
        elif kind in ("pin", "unpin"):
            note = getattr(s, kind)(a["id"])
        elif kind == "accept":
            note = s.accept_suggestion(int(a["index"]))
        elif kind in ("lens", "focus"):
            note = s.apply(kind, a["value"])
            route = "retrieve"
        elif kind == "stage":
            note = s.set_stage(a["value"])
            route = "retrieve"
        elif kind == "next":
            note = s.set_stage(s.suggest_next_stage())
            route = "retrieve"
        elif kind == "close":
            note = s.set_stage(st.CLOSE)
        else:  # free-text question (may itself be a command)
            from ff.engine.lens import parse_command

            cmd = parse_command(a.get("question", ""))
            if cmd is not None:
                note = s.apply(cmd.kind, cmd.arg)
            route = "retrieve"
        stage = s.lens.stage
        if stage == st.CLOSE:
            route = "close"
        asked = a.get("question") if kind == "ask" and not note else None
        if route == "retrieve":
            next_question = asked or st.DEFAULT_QUESTIONS[stage]
        else:
            next_question = state.get("question", "")
        return {"ledger": s.ledger.to_list(), "anchor_ids": list(s.anchor_ids), "lens": s.lens.to_dict(),
                "stage": stage, "question": next_question, "route": route,
                "notes": list(state.get("notes", [])) + ([note] if note else [])}

    def close(state: FFState) -> dict[str, Any]:
        s = session_for(state)
        lines = []
        if deps.store is not None:
            for eid in s.ledger.ids_of("CONFIRMED"):
                deps.store.set_outcome(eid, "root_cause")
            for eid in s.ledger.ids_of("RULED_OUT"):
                deps.store.set_outcome(eid, "ruled_out")
        events = {}
        if deps.store is not None and s.ledger.cited_ids():
            events = {e["id"]: e for e in deps.store.get_events(ids=s.ledger.cited_ids())}
        for ln in s.ledger.lines:
            when = min((events[i]["occurred_at"][:16] for i in ln.ids if i in events), default="")
            lines.append(f"- {when} {ln.render()}".replace("  ", " "))
        lines.sort()
        if deps.store is not None:  # the full RCA report (macro + micro + remediation)
            return {"postmortem": s.rca().markdown}
        text = (f"Postmortem draft: {s.lens.service}, incident {s.lens.incident_start:%Y-%m-%d %H:%M} UTC\n"
                f"Initial question: {s.initial_query}\nTimeline:\n" + ("\n".join(lines) or "- (ledger empty)"))
        return {"postmortem": text}

    # ---------------------------------------------------------------- edges
    g = StateGraph(FFState)
    for name, fn in (("scope", scope), ("pick_anchors", pick_anchors), ("retrieve", retrieve),
                     ("generate", generate), ("engineer", engineer), ("apply_action", apply_action),
                     ("close", close)):
        g.add_node(name, fn)
    g.add_edge(START, "scope")
    g.add_edge("scope", "pick_anchors")
    g.add_edge("pick_anchors", "retrieve")
    g.add_edge("retrieve", "generate")
    g.add_edge("generate", "engineer")
    g.add_edge("engineer", "apply_action")
    g.add_conditional_edges("apply_action", lambda s: s.get("route", "retrieve"),
                            {"retrieve": "retrieve", "engineer": "engineer", "close": "close"})
    g.add_edge("close", END)
    return g.compile(checkpointer=checkpointer)


def sqlite_checkpointer(path: str):
    """SqliteSaver at ``path`` (var/graph_state.sqlite in the app)."""
    import sqlite3

    from langgraph.checkpoint.sqlite import SqliteSaver

    return SqliteSaver(sqlite3.connect(path, check_same_thread=False))


def mermaid(graph: Any) -> str:
    """Mermaid source of the compiled graph (About tab / poster)."""
    return graph.get_graph().draw_mermaid()
