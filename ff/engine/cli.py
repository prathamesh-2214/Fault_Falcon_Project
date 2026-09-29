"""Interactive, step-by-step RCA in the terminal: ``make chat INC=INC-109``.

FaultFalcon answers briefly and asks one question per step; type a reply (or the number of an
option). Useful replies: ``yes``, ``no``, ``next``, an id (``evt_171``, ``chg_147``),
``rule out evt_12``, ``lens storage-svc``, ``no change``, ``report``, or any question.
Other commands: ``ledger`` (show it), ``help``, ``quit``. The RCA report is saved to
``results/rca_<INC>.md`` at the end.

    python -m ff.engine.cli --incident INC-109          # real model
    python -m ff.engine.cli --incident INC-109 --tiny   # offline tiny model (no download)
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ff.config import get_paths
from ff.engine import stages as st
from ff.engine.guide import Guide, Step
from ff.engine.lens import Lens
from ff.engine.session import Session

HELP = ("Replies: yes | no | next | <evt_/chg_ id> | rule out <id> | contributing <id> | lens <service> | "
        "focus <id> | stage <name> | no change | report | any question.  Also: ledger | help | quit")


def render(step: Step, out: Callable[[str], None], streamed: bool = False) -> None:
    """Print one step: notes, the (short) answer, then the question and numbered options."""
    for n in step.notes:
        out(f"  · {n}")
    if step.answer and not streamed:
        out(textwrap.fill(step.answer, 100, initial_indent="FaultFalcon: ", subsequent_indent="             "))
    out(f"\n[{step.n}] {st.LABELS.get(step.stage, step.stage)}  ❓ {step.question}")
    for i, o in enumerate(step.options, 1):
        out(f"    {i}) {o.label}")


def run(incident: str, tiny: bool = False, query: str | None = None, input_fn: Callable[[str], str] = input,
        out: Callable[[str], None] = print, stream: bool = True, max_turns: int = 40) -> dict[str, Any]:
    from ff.ingest.pipeline import open_stores
    from ff.ingest.simulator import load_world
    from ff.llm.model import count_tokens, get_model
    from ff.retrieve.ranker import Ranker

    paths = get_paths()
    inc = next(i for i in load_world(paths).incidents if i["id"] == incident)
    store, vectors = open_stores(paths)
    model, tok = get_model(tiny=tiny)
    ranker = Ranker(store, vectors, count=lambda s: count_tokens(tok, s))
    start = datetime.fromisoformat(inc["incident_start"])
    lens = Lens(inc["service"], start)
    out(f"🦅 FaultFalcon · {incident} · {inc['service']} · anomaly at {inc['incident_start'][:16]} UTC "
        f"(error ratio {inc['anomaly_numbers'].get('peak_error_ratio')} vs baseline "
        f"{inc['anomaly_numbers'].get('baseline_median_error_ratio')})")
    out(HELP)
    q = query or input_fn(f"\nInitial question [{inc['service']} error burst since {start:%H:%M}, what changed?]: ").strip()
    q = q or f"{inc['service']} error burst since {start:%H:%M}, what changed?"
    anchors = [c["id"] for c in ranker.checkpoint_candidates(lens, q) if c["preselect"]][:3]
    out(f"Checkpoints (anchored for the whole session): {', '.join(anchors) or 'none'}")
    session = Session(model, tok, ranker, mode="faultfalcon", store=store, session_id=f"cli-{incident}",
                      token_scale=4 if tiny else 1)
    guide = Guide(session, ranker)

    started = {"on": False}

    def tok_out(t: str) -> None:
        if not started["on"]:  # print the speaker only when the model actually answers
            sys.stdout.write("FaultFalcon: ")
            started["on"] = True
        sys.stdout.write(t)
        sys.stdout.flush()

    def step_with(fn: Callable[..., Step], *args: Any) -> Step:
        if stream and out is print:
            started["on"] = False
            s = fn(*args, on_token=tok_out)
            if started["on"]:
                sys.stdout.write("\n")
            render(s, out, streamed=True)
        else:
            s = fn(*args)
            render(s, out)
        return s

    step = step_with(guide.start, q, lens, anchors)
    for _ in range(max_turns):
        if step.done:
            break
        text = input_fn("\nyou> ").strip()
        low = text.lower()
        if low in ("quit", "exit", "q"):
            break
        if low == "help":
            out(HELP)
            continue
        if low == "ledger":
            out(session.ledger.render())
            continue
        if text.isdigit() and 1 <= int(text) <= len(step.options):
            text = step.options[int(text) - 1].reply
            out(f"you> {text}")
        step = step_with(guide.reply, text)
    rep = session.rca()
    path = paths.results / f"rca_{incident}.md"
    paths.results.mkdir(parents=True, exist_ok=True)
    path.write_text(rep.markdown, encoding="utf-8")
    out(f"\nRCA report ({'complete' if rep.complete else 'draft'}) saved to {path}")
    return {"guide": guide, "report": rep, "path": path, "steps": guide.steps, "incident": inc}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--incident", default="INC-109")
    ap.add_argument("--tiny", action="store_true")
    ap.add_argument("--query", default=None)
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    run(args.incident, args.tiny, args.query)


if __name__ == "__main__":
    main()


