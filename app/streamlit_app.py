"""FaultFalcon app (``run.bat`` / ``make run``): a root-cause analysis copilot for production deployments.

Tabs: Investigation | Code changes | RCA report | Timeline | Logs | Knowledge base (| Evaluation, from Settings).

The investigation runs macro -> micro: find the change that caused the incident (change
timeline, suspects, runtime evidence, hypothesis), then the exact breaking line (diff
analysis), the blast radius and the fix, ending in an RCA report built from the ledger.

Runs from precomputed artifacts (data/, var/, results/). The model is loaded once with
``st.cache_resource``; ``FF_TINY_MODEL=1`` swaps in the offline tiny model (tests / CI).
``USE_LANGGRAPH=true`` drives the chat through the LangGraph flow (ff/engine/graph.py).
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402
import streamlit as st  # noqa: E402

from app import theme  # noqa: E402
from app import ui_helpers as ui  # noqa: E402
from ff import config  # noqa: E402
from ff.config import get_paths  # noqa: E402
from ff.engine import stages as stg  # noqa: E402
from ff.engine.lens import Lens  # noqa: E402

st.set_page_config(page_title="FaultFalcon · Root-cause analysis", page_icon=str(Path(__file__).parent / "assets" / "falcon.svg"), layout="wide",
                   initial_sidebar_state="expanded")
st.markdown(theme.CSS, unsafe_allow_html=True)
PATHS = get_paths()
TOKEN_SCALE = 4 if config.TINY_MODEL else 1  # the offline tiny model counts bytes, not tokens


# --------------------------------------------------------------------------- resources
@st.cache_resource(show_spinner="Opening stores ...")
def stores():
    from ff.store.sqlite_store import SqliteStore
    from ff.store.vector_store import VectorStore

    return SqliteStore(PATHS.sqlite), VectorStore(PATHS.chroma)


@st.cache_resource(show_spinner="Loading the model (first time only) ...")
def model_pair():
    from ff.llm.model import get_model

    return get_model()


@st.cache_resource
def ranker():
    from ff.llm.model import count_tokens
    from ff.retrieve.ranker import Ranker

    store, vectors = stores()
    tok = model_pair()[1]
    return Ranker(store, vectors, count=lambda s: count_tokens(tok, s))


@st.cache_resource
def browse_ranker():
    """A ranker that needs no model (approximate token counts): Diff tab before a session starts."""
    from ff.retrieve.ranker import Ranker

    return Ranker(*stores())


@st.cache_resource
def graph_app():
    from ff.engine.graph import GraphDeps, build_graph, sqlite_checkpointer
    from ff.engine.session import Session

    holder: dict = {"on_token": None}

    def factory(mode, tid):
        m, t = model_pair()
        return Session(m, t, ranker(), mode=mode, store=stores()[0], session_id=tid, token_scale=TOKEN_SCALE)

    deps = GraphDeps(ranker=ranker(), session_factory=factory, store=stores()[0],
                     on_token=lambda tkn: holder["on_token"] and holder["on_token"](tkn))
    PATHS.var.mkdir(parents=True, exist_ok=True)
    return build_graph(deps, sqlite_checkpointer(str(PATHS.graph_state))), holder


@st.cache_data
def windows_df() -> pd.DataFrame | None:
    f = PATHS.derived / "windows.parquet"
    return pd.read_parquet(f) if f.exists() else None


@st.cache_data
def lines_df(service: str) -> pd.DataFrame | None:
    f = PATHS.derived / f"lines_{service}.parquet"
    return pd.read_parquet(f) if f.exists() else None


def ready() -> bool:
    return PATHS.sqlite.exists() and any(PATHS.incidents.glob("INC-*.json"))


# --------------------------------------------------------------------------- state
S = st.session_state
for k, v in (("turns", []), ("session", None), ("candidates", None), ("thread", None), ("pause", None),
             ("run_counter", 0)):
    S.setdefault(k, v)

if not ready():
    st.markdown(theme.topbar(), unsafe_allow_html=True)
    st.error("No incident data found. Start the app with `run.bat` (or run `make ingest` once).")
    st.stop()

INCIDENTS = ui.load_incidents(PATHS)
STORE, VECTORS = stores()

# --------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.markdown(theme.nav_header(), unsafe_allow_html=True)
    st.markdown("**Incident**")
    labels = {k: f"{i['id']} · {i.get('severity', '')} · {i.get('title') or i['service']}" for k, i in enumerate(INCIDENTS)}
    idx = st.selectbox("Incident", range(len(INCIDENTS)), format_func=lambda i: labels[i], key="incident_idx",
                       label_visibility="collapsed")
    INC = INCIDENTS[idx]
    st.markdown("**Scope**")
    lens_service = st.selectbox("Service", config.SERVICES, index=config.SERVICES.index(INC["service"]),
                                key=f"lens_svc_{INC['id']}")
    window_days = st.slider("Look-back window (days)", 3, 30, config.WINDOW_DAYS, key="window_days")
    sess = S.session
    current_stage = sess.lens.stage if sess else stg.SCOPE
    go_cols = st.columns([0.7, 0.3], vertical_alignment="bottom")
    stage_pick = go_cols[0].selectbox("Stage", stg.STAGES, index=stg.STAGES.index(current_stage),
                                      format_func=lambda x: stg.LABELS[x], disabled=sess is None)
    if go_cols[1].button("Go", disabled=sess is None, width="stretch"):
        S.pending = {"type": "stage", "value": stage_pick}
    if st.button("Reset investigation", width="stretch"):
        for k in ("session", "candidates", "thread", "pause", "guide"):
            S[k] = None
        S.turns = []
        st.rerun()
    with st.expander("Settings"):
        guided = st.toggle("Guided Q&A", value=True, key="guided",
                           help="FaultFalcon answers briefly and asks one question per step; reply by clicking or "
                                "typing (yes / no / next / an id / rule out evt_… / lens <service> / any question).")
        mode = st.selectbox("Cache mode", config.CACHE_MODES, index=config.CACHE_MODES.index("faultfalcon"), key="mode")
        use_graph = st.toggle("LangGraph orchestration", value=config.USE_LANGGRAPH, key="use_graph")
        show_eval = st.toggle("Evaluation tab", value=False, key="show_eval")
        reveal = st.toggle("Show resolution", value=False, key="reveal")
    guided = guided and not use_graph
    if reveal:
        c = INC.get("culprit") or {}
        st.info(f"**Resolution:** {INC.get('scenario_title') or ''}  \n"
                f"Root cause `{INC['cause_event_id']}` on {INC.get('cause_service', INC['service'])}  \n"
                f"Breaking line `{INC.get('culprit_change_id')}` {c.get('file', '-')}:{c.get('line', '')}  \n"
                f"Contributing: {', '.join(INC.get('contributing_ids', [])) or '-'}  \n"
                f"Unrelated nearby changes: {', '.join(INC['decoy_ids']) or '-'}")

key = (INC["id"], mode, use_graph, guided)
if S.get("session_key") != key:
    S.session_key, S.session, S.candidates, S.thread, S.pause, S.turns = key, None, None, None, None, []
    S.guide = None

lens0 = Lens(lens_service, datetime.fromisoformat(INC["incident_start"]), window_days=window_days)

# --------------------------------------------------------------------------- header
nums = INC.get("anomaly_numbers", {})
sess = S.session
ledger_confirmed = sess.ledger.ids_of("CONFIRMED") if sess else []
root = next((i for i in ledger_confirmed if i.startswith("evt_")), None)
line = next((i for i in ledger_confirmed if i.startswith("chg_")), None)
status = "Resolved" if sess is not None and sess.lens.stage == stg.CLOSE else ("Investigating" if sess else "Open")
started = INC["incident_start"][:16].replace("T", " ")
st.markdown(theme.topbar(INC["id"]), unsafe_allow_html=True)
st.markdown(theme.title(INC.get("title") or f"Errors on {INC['service']}", INC.get("severity", "SEV-3"), status,
                        f"{INC['service']} · started {started} UTC · {(INC.get('detection') or {}).get('alert', '')}"),
            unsafe_allow_html=True)
st.markdown(theme.details([
    ("Incident ID", INC["id"], ""),
    ("Alerting service", INC["service"], ""),
    ("Started (UTC)", started, ""),
    ("Error rate", f"{nums.get('peak_error_ratio', 0):.0%} (baseline {nums.get('baseline_median_error_ratio', 0):.0%})",
     "warn"),
    ("Root cause", root or "Not identified", "ok" if root else ""),
    ("Breaking line", line or "Not identified", "ok" if line else ""),
]), unsafe_allow_html=True)
st.markdown(theme.stepper(sess.lens.stage if sess else stg.SCOPE), unsafe_allow_html=True)

TABS = ["Investigation", "Code changes", "RCA report", "Timeline", "Logs", "Knowledge base"]
tabs = st.tabs(TABS + (["Evaluation"] if show_eval else []))
tab_chat, tab_diff, tab_rca, tab_timeline, tab_logs, tab_kb = tabs[:6]
tab_exp = tabs[6] if show_eval else None


def records_table(rows: list[dict]) -> pd.DataFrame:
    """'Why these records' without bookkeeping columns."""
    return pd.DataFrame(rows).drop(columns=["source", "tag"], errors="ignore")


def styled(fig):
    """Plotly figures on the console background."""
    fig.update_layout(paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)", font_color=theme.TEXT)
    return fig


# --------------------------------------------------------------------------- chat helpers
def new_session():
    from ff.engine.session import Session

    m, t = model_pair()
    return Session(m, t, ranker(), mode=mode, store=STORE, session_id=f"{INC['id']}-{mode}", token_scale=TOKEN_SCALE)


def record_turn(res) -> None:
    S.turns.append({"turn": res.turn, "question": res.question, "answer": res.answer, "stage": res.stage,
                    "explanations": res.explanations, "context_ids": res.context_ids, "unverified": res.unverified,
                    "gen_stats": res.gen_stats, "cache_stats": res.cache_stats, "rebuild": res.rebuild,
                    "notes": res.notes})


def run_turn(question: str) -> None:
    """Plain session loop: retrieve + stream the answer."""
    sess = S.session
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        box, status = st.empty(), st.empty()
        buf: list[str] = []

        def on_token(t: str) -> None:
            buf.append(t)
            if len(buf) % 4 == 0:
                box.markdown("".join(buf) + " ▌")
                status.caption(f"cache: {sess.cache_tokens()} tokens · {len(buf)} pieces")

        with st.spinner(f"{stg.LABELS[sess.lens.stage]}: retrieving and generating ..."):
            res = sess.turn(question, on_token=on_token)
        box.markdown(res.answer)
        status.empty()
    record_turn(res)


def graph_invoke(payload, new: bool = False):
    from langgraph.types import Command

    graph, holder = graph_app()
    cfg = {"configurable": {"thread_id": S.thread}}
    box = st.empty()
    buf: list[str] = []
    holder["on_token"] = lambda t: (buf.append(t), box.markdown("".join(buf) + " ▌"))
    with st.spinner("Working ..."):
        out = graph.invoke(payload if new else Command(resume=payload), cfg)
    holder["on_token"] = None
    box.empty()
    S.pause = out["__interrupt__"][0].value if out.get("__interrupt__") else None
    S.graph_out = out
    from ff.engine import cache_registry

    S.session = cache_registry.get(S.thread)
    if S.pause and S.pause.get("type") == "answer" and out.get("answer") is not None:
        if not S.turns or S.turns[-1]["turn"] != out.get("turn"):
            S.turns.append({"turn": out.get("turn"), "question": out.get("question", ""), "answer": out["answer"],
                            "stage": out.get("stage"), "explanations": out.get("explanations", []),
                            "context_ids": out.get("context_ids", []), "unverified": out.get("unverified", []),
                            "gen_stats": out.get("gen_stats", {}), "cache_stats": out.get("cache_stats", {}),
                            "rebuild": None, "notes": out.get("notes", [])})
    return out


def guide_step(fn, *args) -> None:
    """Run one guided step with the answer streamed into a chat bubble."""
    with st.chat_message("assistant", avatar="🦅"):
        box = st.empty()
        buf: list[str] = []

        def on_token(t: str) -> None:
            buf.append(t)
            if len(buf) % 3 == 0:
                box.markdown("".join(buf) + " ▌")

        with st.spinner("Thinking ..."):
            step = fn(*args, on_token=on_token)
        box.empty()
    if step.turn is not None:
        record_turn(step.turn)


def guide_reply(text: str) -> None:
    """The engineer's reply (typed or clicked) in guided mode."""
    with st.chat_message("user"):
        st.markdown(text)
    guide_step(S.guide.reply, text)


def as_reply(action: dict) -> str | None:
    """The engineer reply a button stands for (guided mode)."""
    kind = action["type"]
    if kind in ("confirm", "drill"):
        return f"confirm {action['id']}"
    if kind == "rule_out":
        return f"rule out {action['id']}"
    if kind in ("refute", "contributing", "pin", "unpin"):
        return f"{kind} {action['id']}"
    if kind == "stage":
        return "report" if action["value"] == stg.CLOSE else f"stage {action['value'].lower()}"
    if kind == "ask":
        return action["question"]
    return None


def act(action: dict) -> None:
    """Apply an engineer action (button / command) in either orchestration."""
    sess = S.session
    if use_graph:
        graph_invoke(action)
        return
    if S.get("guide") is not None:
        text = as_reply(action)
        if text is not None:
            guide_reply(text)
        elif action["type"] == "accept":
            st.toast(sess.accept_suggestion(action["index"]))
        return
    kind = action["type"]
    if kind in ("confirm", "refute", "rule_out", "pin", "unpin", "contributing"):
        st.toast(getattr(sess, kind)(action["id"]))
    elif kind == "drill":
        st.toast(sess.drill(action["id"]))
        run_turn(stg.DEFAULT_QUESTIONS[stg.DIFF_ANALYSIS])
    elif kind == "stage":
        sess.set_stage(action["value"])
        if action["value"] != stg.CLOSE:
            run_turn(stg.DEFAULT_QUESTIONS[action["value"]])
    elif kind == "accept":
        st.toast(sess.accept_suggestion(action["index"]))
    elif kind == "ask":
        run_turn(action["question"])


def suggested_actions(sess) -> list[tuple[str, dict]]:
    """Stage-aware one-click next steps (the demo can be clicked through without typing)."""
    stage = sess.lens.stage
    last = S.turns[-1] if S.turns else None
    out: list[tuple[str, dict]] = []
    confirmed = sess.ledger.ids_of("CONFIRMED")
    if stage in (stg.CHANGE_TIMELINE, stg.SUSPECTS, stg.RUNTIME, stg.HYPOTHESIS, stg.SCOPE):
        cited = []
        if last:
            from ff.engine.session import cited_ids

            answer_ids = [i for i in cited_ids(last["answer"]) if i.startswith("evt_")]
            cited = answer_ids + [i for i in last["context_ids"] if i.startswith("evt_")]
        store_evs = {e["id"]: e for e in STORE.get_events(ids=list(dict.fromkeys(cited)))}
        is_change = [i for i in dict.fromkeys(cited) if store_evs.get(i, {}).get("event_type") in config.CHANGE_TYPES]
        # the model's own citations first, then the most recent changes in the context
        cited_first = [i for i in is_change if last and f"[{i}]" in last["answer"]]
        newest = sorted((i for i in is_change if i not in cited_first),
                        key=lambda i: store_evs[i]["occurred_at"], reverse=True)
        changes = cited_first + newest
        for eid in changes[:3]:
            out.append((f"🎯 Confirm {eid} as root cause → find the breaking line", {"type": "drill", "id": eid}))
        # cross-service incidents: follow the dependency chain one hop at a time
        hops = list(config.dependencies(sess.lens.service))
        if config.host_of(sess.lens.service):
            hops.append(config.host_of(sess.lens.service))
        for dep in hops[:2]:
            out.append((f"🔗 Follow dependency → {dep}", {"type": "ask", "question": f"lens {dep}"}))
        nxt = sess.suggest_next_stage()
        out.append((f"Next: {stg.LABELS[nxt]}", {"type": "stage", "value": nxt}))
        out.append(("Which change best explains the errors?",
                    {"type": "ask", "question": "Which change best explains the error signatures?"}))
    elif stage == stg.DIFF_ANALYSIS:
        targets = ranker().target_changes(sess.lens, sess.ledger.render(sess.count))
        hunks = ranker().suspect_hunks(sess.lens, targets) if targets else []
        for h in hunks[:1]:
            if h["change_id"] not in confirmed:
                out.append((f"🎯 Confirm {h['change_id']} ({h['file'].split('/')[-1]}:{h['line']}) as the breaking line",
                            {"type": "confirm", "id": h["change_id"]}))
        out.append((f"Next: {stg.LABELS[stg.BLAST_RADIUS]}", {"type": "stage", "value": stg.BLAST_RADIUS}))
    elif stage == stg.BLAST_RADIUS:
        out.append((f"Next: {stg.LABELS[stg.REMEDIATION]}", {"type": "stage", "value": stg.REMEDIATION}))
    elif stage == stg.REMEDIATION:
        out.append(("📝 Finish: build the RCA report", {"type": "stage", "value": stg.CLOSE}))
    return out


def render_guided(sess) -> None:
    """The step-by-step dialogue: short answers, one question per step, clickable answers."""
    steps = S.guide.steps
    for stp in steps:
        if stp.engineer:
            with st.chat_message("user"):
                st.markdown(stp.engineer)
        with st.chat_message("assistant", avatar="🦅"):
            st.markdown(f"<span class='ff-pill'>Step {stp.n} · {stg.LABELS.get(stp.stage, stp.stage)}</span>",
                        unsafe_allow_html=True)
            for n in stp.notes:
                st.caption(f"✔ {n}")
            if stp.answer:
                st.markdown(stp.answer)
            if stp.turn is not None:
                if stp.turn.unverified:
                    st.caption(f"⚠️ unverified citations: {', '.join(stp.turn.unverified)}")
                with st.expander("Evidence used"):
                    if stp.turn.explanations:
                        st.dataframe(records_table(stp.turn.explanations), hide_index=True, width="stretch")
                    else:
                        st.caption("No records retrieved.")
            st.info(stp.question, icon="❓")
    if steps and steps[-1].options:
        last = steps[-1]
        cols = st.columns(min(3, len(last.options)))
        for i, o in enumerate(last.options):
            if cols[i % len(cols)].button(o.label, key=f"opt_{last.n}_{i}", width="stretch"):
                guide_reply(o.reply)
                st.rerun()
    if steps and steps[-1].done:
        st.success("Investigation closed. The RCA report is ready in the RCA report tab.")


# --------------------------------------------------------------------------- tab: investigation
with tab_chat:
    if S.get("guide") is not None and S.get("guide_start"):
        q0, lens_start, chosen0 = S.pop("guide_start")
        with st.chat_message("user"):
            st.markdown(q0)
        guide_step(S.guide.start, q0, lens_start, chosen0)
        S.session = S.guide.s
        st.rerun()
    sess = S.session
    if sess is None:
        st.subheader("Start an investigation")
        c1, c2 = st.columns([0.78, 0.22], vertical_alignment="bottom")
        q = c1.text_input("Question", ui.default_query(INC), key=f"iq_{INC['id']}", max_chars=400)
        find = c2.button("Find checkpoint candidates", type="secondary", width="stretch")
        if find:
            if use_graph:
                S.thread = f"{INC['id']}#{S.run_counter}"
                S.run_counter += 1
                graph_invoke({"thread_id": S.thread, "initial_query": q, "lens": lens0.to_dict(), "ledger": [],
                              "turn": 0, "cache_mode": mode}, new=True)
                S.candidates = (S.pause or {}).get("candidates", [])
            else:
                S.candidates = ranker().checkpoint_candidates(lens0, q)
        if S.candidates is None:
            st.caption("FaultFalcon proposes the changes and signals worth keeping in view, then walks from the "
                       "change timeline to the exact line that broke production.")
        else:
            st.markdown(f"**Choose checkpoints** · pinned for the whole investigation (up to {config.MAX_CHECKPOINTS})")
            chosen = []
            with st.container(border=True, height=250):
                for c in S.candidates:
                    cols = st.columns([0.04, 0.96])
                    if cols[0].checkbox(" ", value=c["preselect"], key=f"ck_{INC['id']}_{c['id']}",
                                        label_visibility="collapsed"):
                        chosen.append(c["id"])
                    cols[1].markdown(f"`{c['compressed']}`  \n<span style='opacity:.7'>{c['reason']}</span>",
                                     unsafe_allow_html=True)
            anchor_tokens = ranker().count("Initial question: " + q) + sum(
                ranker().count(c["compressed"]) for c in S.candidates if c["id"] in chosen)
            budget = config.SEGMENT_BUDGETS["initial_query"] + config.SEGMENT_BUDGETS["checkpoints"]
            too_many = len(chosen) > config.MAX_CHECKPOINTS
            b1, b2 = st.columns([0.25, 0.75], vertical_alignment="center")
            start = b1.button("Start investigation", type="primary", disabled=too_many, width="stretch")
            b2.progress(min(1.0, anchor_tokens / budget), text=f"Pinned context: {anchor_tokens} / {budget} tokens")
            if too_many:
                st.warning(f"Pick at most {config.MAX_CHECKPOINTS} checkpoints.")
            if start:
                if use_graph:
                    graph_invoke(chosen)
                elif guided:  # the session becomes visible (S.session) once its first step has run
                    from ff.engine.guide import Guide

                    S.guide = Guide(new_session(), ranker())
                    S.guide_start = (q, Lens(**{**lens0.__dict__, "stage": stg.CHANGE_TIMELINE}), chosen)
                else:
                    S.session = new_session()
                    S.session.start(q, Lens(**{**lens0.__dict__, "stage": stg.CHANGE_TIMELINE}), chosen)
                    S.first_question = q
                st.rerun()
    else:
        if not use_graph and not S.turns and S.get("first_question"):
            run_turn(S.pop("first_question"))
            st.rerun()  # re-render from history (otherwise the streamed turn shows twice)
        main, right = st.columns([0.66, 0.34])
        with main:
            if S.get("guide") is not None:
                render_guided(sess)
            for t in ([] if S.get("guide") is not None else S.turns):
                with st.chat_message("user"):
                    st.markdown(f"<span class='ff-pill'>{stg.LABELS.get(t['stage'], t['stage'])}</span> {t['question']}",
                                unsafe_allow_html=True)
                with st.chat_message("assistant"):
                    st.markdown(t["answer"] or "_(empty answer)_")
                    if t["unverified"]:
                        st.caption(f"⚠️ unverified citations: {', '.join(t['unverified'])}")
                    with st.expander("Evidence used"):
                        if t["explanations"]:
                            st.dataframe(records_table(t["explanations"]), hide_index=True, width="stretch")
                        else:
                            st.caption("No records retrieved.")
                        for eid in t["context_ids"]:
                            if eid.startswith("evt_") and eid not in sess.anchor_ids and st.button(
                                    f"Pin {eid} as checkpoint", key=f"pin_{t['turn']}_{eid}"):
                                act({"type": "pin", "id": eid})
                                st.rerun()
            sugg = [] if S.get("guide") is not None else suggested_actions(sess)
            if sugg:
                st.markdown("**Next steps**")
            cols = st.columns(max(1, min(3, len(sugg))))
            for n, (label, action) in enumerate(sugg):
                if cols[n % len(cols)].button(label, key=f"sugg_{n}_{sess.lens.stage}", width="stretch"):
                    act(action)
                    st.rerun()
            if sess.lens.stage == stg.CLOSE and S.get("guide") is None:
                st.success("Investigation closed. The RCA report is ready in the RCA report tab.")
            placeholder = ("Reply: yes · no · next · an id · rule out evt_… · lens <service> · or ask a question"
                           if S.get("guide") is not None else
                           "Ask, or type: lens storage-svc · focus evt_12 · stage diff · pin evt_7")
            if (q := st.chat_input(placeholder)):
                act({"type": "ask", "question": q})
                st.rerun()
        with right:
            with st.container(border=True):
                st.markdown("**Findings**")
                if sess.ledger.lines:
                    for ln in sess.ledger.lines:
                        st.markdown(f"- {ln.render()}")
                else:
                    st.caption("Nothing confirmed yet.")
                for i, sug in enumerate(sess.ledger.suggestions):
                    c1, c2 = st.columns([0.8, 0.2])
                    c1.caption(f"Suggestion: {sug.text}")
                    if c2.button("Accept", key=f"acc_{i}"):
                        act({"type": "accept", "index": i})
                        st.rerun()
            cited = list(dict.fromkeys(i for t in S.turns[-1:] for i in t["context_ids"]))
            if cited:
                with st.expander("Records in the last answer", expanded=S.get("guide") is None):
                    for eid in cited[:8]:
                        c = st.columns([0.28, 0.18, 0.18, 0.18, 0.18])
                        c[0].markdown(f"`{eid}`")
                        for col, (kind, label) in zip(c[1:], (("confirm", "Confirm"), ("contributing", "Contrib."),
                                                              ("refute", "Refute"), ("rule_out", "Ruled out"))):
                            if col.button(label, key=f"{kind}_{eid}"):
                                act({"type": kind, "id": eid})
                                st.rerun()
            with st.expander("Pinned context"):
                st.markdown(f"> {sess.initial_query}")
                for eid in list(sess.anchor_ids):
                    c1, c2 = st.columns([0.8, 0.2])
                    c1.markdown(f"`{ranker().compress_id(eid)}`")
                    if c2.button("Unpin", key=f"unpin_{eid}"):
                        act({"type": "unpin", "id": eid})
                        st.rerun()
            cds = STORE.get_change_details([i for i in cited if i.startswith("evt_")])
            if cds:
                with st.expander("Files to inspect"):
                    st.dataframe(ui.files_to_inspect(cds), hide_index=True, width="stretch")
            with st.expander("Session internals"):
                st.plotly_chart(styled(ui.kv_bar(sess.cache_stats())), width="stretch", key="kvbar")
                rb = sess.cache.last_rebuild if sess.cache is not None else None
                if rb:
                    st.caption(f"Last rebuild: **{rb.get('segment')}** · {rb.get('tokens_reencoded')} tokens "
                               f"re-encoded · {rb.get('ms', 0):.0f} ms")
                if S.turns:
                    st.plotly_chart(styled(ui.latency_spark(S.turns)), width="stretch", key="spark")
        if S.get("pending"):
            act(S.pop("pending"))
            st.rerun()

# --------------------------------------------------------------------------- tab: diff & suspect lines
with tab_diff:
    sess = S.session
    rk = ranker() if sess is not None else browse_ranker()
    lens_now = sess.lens if sess is not None else lens0
    ledger_text = sess.ledger.render(sess.count) if sess is not None else ""
    ranked = rk.ranked_changes(lens_now, "what changed before the errors?")
    default = [e["id"] for e in rk.target_changes(lens_now, ledger_text)]
    options = list(dict.fromkeys(default + [e["id"] for e in ranked[:25]]))
    if not options:
        st.info("No prod changes in the lens window.")
    else:
        evs = {e["id"]: e for e in STORE.get_events(ids=options)}
        pick = st.selectbox("Change to inspect", options, format_func=lambda i: rk.compress(evs[i]),
                            key=f"diffpick_{INC['id']}")
        st.caption("Diff hunks ranked by how likely each one broke production, against the incident's error "
                   "signatures.")
        hunks = rk.suspect_hunks(lens_now, [evs[pick]])
        anomaly, evidence = rk.incident_evidence(lens_now)
        if anomaly:
            st.markdown(f"**Error signatures** · `{anomaly['id']}`: " + " · ".join(
                f"`{t[:70]}`" for t in (anomaly["payload"].get("templates") or [])[:3]))
        for n, h in enumerate(hunks):
            with st.container(border=True):
                top = st.columns([0.62, 0.38])
                top[0].markdown(f"**#{n + 1} `{h['change_id']}`** · `{h['file']}:{h['line']}` · {h['symbol']}  \n"
                                f"<span class='ff-pill'>in {h['event_id']}</span> "
                                + " ".join(f"<span class='ff-pill'>{r}</span>" for r in h["reasons"]),
                                unsafe_allow_html=True)
                top[1].markdown(f"suspicion **{h['score']:.2f}**" + theme.score_bar(h["score"]), unsafe_allow_html=True)
                st.markdown(theme.diff_html(h["diff"]), unsafe_allow_html=True)
                if sess is not None:
                    b = st.columns(3)
                    if b[0].button("Confirm as breaking line", key=f"cfl_{h['change_id']}"):
                        act({"type": "confirm", "id": h["change_id"]})
                        st.rerun()
                    if b[1].button("Contributing", key=f"cfc_{h['change_id']}"):
                        act({"type": "contributing", "id": h["change_id"]})
                        st.rerun()
                    if b[2].button("Rule out", key=f"cfr_{h['change_id']}"):
                        act({"type": "rule_out", "id": h["change_id"]})
                        st.rerun()
        if sess is None:
            st.caption("Start an investigation to confirm lines into the ledger.")

# --------------------------------------------------------------------------- tab: RCA report
with tab_rca:
    sess = S.session
    if sess is None:
        st.info("Start an investigation. The report is built from what you confirm: the root-cause change, the "
                "breaking line, contributing factors and ruled-out changes.")
    else:
        rep = sess.rca()
        c = st.columns([0.7, 0.3])
        c[0].markdown(theme.badge("Complete" if rep.complete else "Draft", "done" if rep.complete else "progress")
                      + f" &nbsp; root cause `{rep.root_cause_id or 'open'}` · breaking line "
                        f"`{rep.breaking_change_id or 'open'}`", unsafe_allow_html=True)
        c[1].download_button("Download RCA report (.md)", rep.markdown, file_name=f"RCA-{INC['id']}.md",
                             mime="text/markdown", width="stretch")
        with st.container(border=True):
            st.markdown(rep.markdown)

# --------------------------------------------------------------------------- tab: knowledge base
with tab_kb:
    from ff.ingest.cycles import cycle_id

    kb = PATHS.docs_arch
    k1, k0, k2, k3, k4 = st.tabs(["Release trains", "Change log", "Codebase", "Postmortems", "Architecture"])
    with k1:
        cyc = sorted(p.stem for p in (kb / "releases").glob("*.md"))
        if cyc:
            cur = cycle_id(datetime.fromisoformat(INC["incident_start"]))
            pick_c = st.selectbox("Release train", cyc, index=cyc.index(cur) if cur in cyc else len(cyc) - 1,
                                  key="kb_cycle")
            st.caption(f"The incident happened during {cur}.")
            st.markdown((kb / "releases" / f"{pick_c}.md").read_text(encoding="utf-8"))
        else:
            st.info("No release notes yet: run `make ingest`.")
    with k0:
        days = sorted(p.stem for p in (kb / "changelog").glob("*.md"))
        if days:
            day = INC["incident_start"][:10]
            before = [d for d in days if d <= day] or days
            pick_d = st.selectbox("Day", days, index=days.index(before[-1]), key="kb_day")
            st.markdown((kb / "changelog" / f"{pick_d}.md").read_text(encoding="utf-8"))
        else:
            st.info("No change log yet: run `make ingest`.")
    with k2:
        svc_kb = st.selectbox("Service", config.SERVICES, index=config.SERVICES.index(lens_service), key="kb_svc")
        f = kb / "codebase" / f"{svc_kb}.md"
        st.markdown(f.read_text(encoding="utf-8") if f.exists() else "No codebase guide yet: run `make ingest`.")
    with k3:
        from ff.ingest.doc_indexer import doc_published

        pms = sorted((kb / "postmortems").glob("*.md"))
        if not pms:
            st.info("No postmortems yet: run `make ingest`.")
        t_inc = datetime.fromisoformat(INC["incident_start"])
        known = [(p, p.read_text(encoding="utf-8")) for p in pms]
        known = [(p, t) for p, t in known if doc_published(t) <= t_inc]
        st.caption(f"{len(known)} postmortems published before this incident.")
        for p, text in known[-40:]:
            with st.expander(p.stem.replace("__", " · ")):
                st.markdown(text)
    with k4:
        for name in ("topology.md", "patterns.md"):
            f = kb / name
            if f.exists():
                st.markdown(f.read_text(encoding="utf-8"))

# --------------------------------------------------------------------------- tab: timeline
with tab_timeline:
    services = list(config.neighbourhood(lens_service))
    b = STORE.get_baseline(lens_service)
    if b:
        lat = (f"{b['p95_latency_band'][0]:.2f}–{b['p95_latency_band'][1]:.2f} s" if b.get("p95_latency_band")
               else "n/a (no latency in these logs)")
        with st.container(border=True):
            st.markdown(f"**Normal behaviour of {lens_service}** · error ratio median "
                        f"**{b['error_ratio_median']:.2f}**, band **{b['error_ratio_band'][0]:.2f}–"
                        f"{b['error_ratio_band'][1]:.2f}** · line rate **{b['rate_band'][0]:.1f}–"
                        f"{b['rate_band'][1]:.1f}/min** · p95 latency **{lat}**")
    events = STORE.get_events(services=services)
    st.plotly_chart(styled(ui.timeline_figure(events, INC, services, reveal, window_days)), width="stretch")
    st.caption("Markers: changes (colour = type, size = impact). Red bands: detected anomalies. Dashed line: "
               "incident start.")

# --------------------------------------------------------------------------- tab: logs
with tab_logs:
    svc = st.selectbox("Service", config.SERVICES, index=config.SERVICES.index(lens_service), key="logs_svc")
    st.caption(config.COMPONENTS[svc].aws)
    w = windows_df()
    if w is not None:
        st.plotly_chart(styled(ui.error_ratio_figure(w, svc)), width="stretch")
    lines = lines_df(svc)
    if lines is not None:
        with st.expander("Log templates", expanded=False):
            st.dataframe(ui.template_table(lines), hide_index=True, width="stretch")
    with st.expander("Detected anomalies", expanded=True):
        t_inc = INC["incident_start"]
        anomalies = sorted(STORE.get_events(services=[svc], types=["ANOMALY"]),
                           key=lambda a: abs(datetime.fromisoformat(a["occurred_at"]) -
                                             datetime.fromisoformat(t_inc)).total_seconds())
        for a in anomalies[:15]:
            st.markdown(f"- `{a['id']}` {a['narrative']}")

# --------------------------------------------------------------------------- tab: evaluation (Settings)
if tab_exp is not None:
    with tab_exp:
        from ff.eval import report

        shown = False
        for name, loader, fig_fn, take_fn in report.SECTIONS:
            df = loader(PATHS)
            if df is None:
                continue
            shown = True
            st.subheader(name)
            for fig in fig_fn(df):
                st.plotly_chart(styled(fig), width="stretch")
            for sentence in take_fn(df):
                st.markdown(f"- {sentence}")
        det = PATHS.results / "detector_bgl.csv"
        if det.exists():
            d = pd.read_csv(det).iloc[0]
            st.subheader("Anomaly detector (BGL labels)")
            st.markdown(theme.details([("Precision", f"{d['precision']:.2f}", ""), ("Recall", f"{d['recall']:.2f}", ""),
                                       ("F1", f"{d['f1']:.2f}", "")], heading="Window level"), unsafe_allow_html=True)
        if not shown:
            st.info("No evaluation results yet: run `make micro-check`, `make exp-a`, `make exp-b` and `make replay`.")

# --------------------------------------------------------------------------- footer
st.markdown(theme.footer(config.MODEL_LABEL), unsafe_allow_html=True)
