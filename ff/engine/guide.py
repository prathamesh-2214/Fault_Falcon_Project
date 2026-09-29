"""Guided, step-by-step RCA: short answers, one question back to the engineer per step.

The investigation becomes a short Q-A dialogue. Each step:

1. FaultFalcon answers briefly (at most 3 sentences) for the current stage,
2. then asks the engineer ONE focused question about a concrete candidate
   ("Should we examine [evt_171]?", "Is [chg_147] BlockWriter.java:32 the line that broke it?"),
3. and the engineer replies by clicking an option or typing anything.

Replies understood (case-insensitive):

==========================  ==================================================================
``yes`` / ``y`` / ``ok``    accept the candidate: examine it, confirm it as the root cause (and
                            drill into its diff), confirm the breaking line, accept the fix
``no`` / ``n``              reject it: it is ruled out and the next candidate is asked about
``next`` / ``continue``     move to the next stage
``evt_123`` / ``chg_45``    look at / confirm that specific change or line instead
``rule out evt_12``         ledger actions: ``rule out``, ``confirm``, ``contributing`` + an id
``lens storage-svc``        any redirect command: ``lens``, ``focus``, ``stage``, ``pin``, ``unpin``
``no change``               at the hypothesis: no change explains the incident
``report``                  finish and write the RCA report now
anything else               a free question, answered briefly within the current stage
==========================  ==================================================================

The dialogue logic is UI-independent: the Streamlit app and the terminal (``ff.engine.cli``)
both drive a :class:`Guide`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from ff import config
from ff.engine import stages as st
from ff.engine.lens import Lens, parse_command
from ff.engine.session import Session, TurnResult, cited_ids

YES = {"yes", "y", "yeah", "yep", "ok", "okay", "sure", "correct", "confirm", "confirmed", "agree", "go", "examine"}
NO = {"no", "n", "nope", "wrong", "not it", "reject"}
NEXT = {"next", "continue", "go on", "skip", "move on", "proceed"}
_IDS = re.compile(r"\b((?:evt|chg)_\d+)\b")
_VERB = re.compile(r"^(rule out|ruled out|refute|confirm|contributing|contributes)\s+((?:evt|chg)_\d+)$")


@dataclass
class Option:
    """A clickable answer: ``reply`` is sent exactly as if the engineer had typed it."""

    label: str
    reply: str


@dataclass
class Step:
    """One exchange of the dialogue."""

    n: int
    stage: str
    engineer: str
    answer: str
    question: str
    options: list[Option] = field(default_factory=list)
    candidate: str | None = None
    notes: list[str] = field(default_factory=list)
    turn: TurnResult | None = None
    done: bool = False


class Guide:
    """Drives a :class:`Session` as a short, sequential Q-A with the engineer."""

    def __init__(self, session: Session, ranker: Any, max_new_tokens: int = 96) -> None:
        self.s = session
        self.r = ranker
        session.brief = True
        session.max_new_tokens = min(session.max_new_tokens, max_new_tokens)
        self.steps: list[Step] = []
        self.candidate: str | None = None
        self.done = False

    # ------------------------------------------------------------------ public API
    def start(self, initial_query: str, lens: Lens, anchor_ids: list[str],
              on_token: Callable[[str], None] | None = None) -> Step:
        """Start the session (anchor block) and answer the initial question."""
        self.s.start(initial_query, replace(lens, stage=st.CHANGE_TIMELINE), anchor_ids)
        return self._run(initial_query, initial_query, on_token=on_token)

    def reply(self, text: str, on_token: Callable[[str], None] | None = None) -> Step:
        """Apply the engineer's reply and run the next step."""
        t = " ".join(text.strip().split())
        low = t.lower().rstrip(".!")
        stage = self.s.lens.stage
        c = self.candidate
        if not t:
            return self._reask("", [])
        if low in ("report", "finish", "done", "write the report"):
            return self._finish(t, [])
        cmd = parse_command(t)
        if cmd is not None:
            note = self.s.apply(cmd.kind, cmd.arg)
            if cmd.kind == "lens":
                note = f"Lens moved to {cmd.arg}: its changes, dependencies and host are now in view."
            elif cmd.kind == "focus":
                note = f"Focus on [{cmd.arg}]."
            elif cmd.kind == "stage":
                note = f"Stage: {st.LABELS[cmd.arg]}."
            q = st.DEFAULT_QUESTIONS[self.s.lens.stage]
            return self._run(t, q, [note], on_token)
        m = _VERB.match(low)
        if m:
            return self._verb(t, m.group(1), m.group(2), on_token)
        ids = _IDS.findall(low)
        if ids and _IDS.sub("", low).strip(" ,.") in ("", "check", "look at", "examine", "this", "it is"):
            return self._pick(t, ids[0], on_token)
        if low in NEXT:
            nxt = st.next_stage(stage)
            if nxt == st.CLOSE:
                return self._finish(t, [])
            self.s.set_stage(nxt)
            return self._run(t, st.DEFAULT_QUESTIONS[nxt], on_token=on_token)
        if "no change" in low and stage in st.MACRO_STAGES:
            self.s.ledger.add_open("No change explains the anomaly (engineer's conclusion).")
            return self._finish(t, ["Concluded: no change explains it."])
        if low in YES and c:
            return self._yes(t, c, on_token)
        if low in NO and c:
            return self._no(t, c, on_token)
        if low in YES | NO:  # nothing to accept or reject: move on
            return self.reply("next", on_token)
        return self._run(t, t, on_token=on_token)  # a free question within the current stage

    # ------------------------------------------------------------------ reply handlers
    def _yes(self, t: str, c: str, on_token: Callable[[str], None] | None) -> Step:
        stage = self.s.lens.stage
        if stage == st.CHANGE_TIMELINE and c.startswith("evt_"):
            return self._examine(t, c, on_token)
        if stage in st.MACRO_STAGES and c.startswith("evt_"):
            note = self.s.drill(c)
            return self._run(t, st.DEFAULT_QUESTIONS[st.DIFF_ANALYSIS], [note], on_token)
        if stage == st.DIFF_ANALYSIS and c.startswith("chg_"):
            note = self.s.confirm(c)
            self.s.set_stage(st.BLAST_RADIUS)
            return self._run(t, st.DEFAULT_QUESTIONS[st.BLAST_RADIUS], [note.replace("root cause", "breaking line")],
                             on_token)
        if stage == st.REMEDIATION:
            return self._finish(t, ["Fix accepted."])
        return self.reply("next", on_token)

    def _no(self, t: str, c: str, on_token: Callable[[str], None] | None) -> Step:
        note = self.s.rule_out(c)
        if self.s.lens.focus == c:
            self.s.set_lens(replace(self.s.lens, focus=None))
        self.candidate = self._candidate(None)
        return self._reask(t, [note])

    def _verb(self, t: str, verb: str, eid: str, on_token: Callable[[str], None] | None) -> Step:
        if verb in ("rule out", "ruled out", "refute"):
            note = self.s.rule_out(eid) if verb != "refute" else self.s.refute(eid)
            if self.candidate == eid:
                self.candidate = self._candidate(None)
            return self._reask(t, [note])
        if verb in ("contributing", "contributes"):
            return self._reask(t, [self.s.contributing(eid)])
        # confirm
        if eid.startswith("evt_") and self.s.lens.stage in st.MACRO_STAGES:
            note = self.s.drill(eid)
            return self._run(t, st.DEFAULT_QUESTIONS[st.DIFF_ANALYSIS], [note], on_token)
        note = self.s.confirm(eid)
        if eid.startswith("chg_") and self.s.lens.stage == st.DIFF_ANALYSIS:
            self.s.set_stage(st.BLAST_RADIUS)
            return self._run(t, st.DEFAULT_QUESTIONS[st.BLAST_RADIUS], [note], on_token)
        return self._reask(t, [note])

    def _pick(self, t: str, eid: str, on_token: Callable[[str], None] | None) -> Step:
        stage = self.s.lens.stage
        if eid.startswith("chg_"):
            if stage == st.DIFF_ANALYSIS:
                return self._yes(t, eid, on_token)
            return self._reask(t, [self.s.confirm(eid)])
        if stage in st.MICRO_STAGES:  # a new change while in micro: drill into it instead
            note = self.s.drill(eid)
            return self._run(t, st.DEFAULT_QUESTIONS[st.DIFF_ANALYSIS], [note], on_token)
        return self._examine(t, eid, on_token)

    def _examine(self, t: str, eid: str, on_token: Callable[[str], None] | None) -> Step:
        self.s.set_lens(replace(self.s.lens, focus=eid, stage=st.SUSPECTS))
        return self._run(t, f"Could [{eid}] explain the symptom?", on_token=on_token)

    def _finish(self, t: str, notes: list[str]) -> Step:
        self.s.set_stage(st.CLOSE)
        rep = self.s.rca()
        self.done = True
        root = rep.root_cause_id or "not confirmed"
        line = rep.breaking_change_id or "not confirmed"
        answer = f"RCA report written. Root cause: [{root}]. Breaking line: [{line}]." if rep.root_cause_id else \
            "RCA report written (no confirmed root cause)."
        step = Step(len(self.steps) + 1, st.CLOSE, t, answer, "The investigation is closed. Open the RCA report, "
                    "or type 'lens <service>' / a question to reopen it.", [], None, notes, None, True)
        self.steps.append(step)
        return step

    # ------------------------------------------------------------------ steps
    def _run(self, engineer: str, question: str, notes: list[str] | None = None,
             on_token: Callable[[str], None] | None = None) -> Step:
        self.done = False
        res = self.s.turn(question, on_token=on_token)
        self.candidate = self._candidate(res)
        q, opts = self._ask()
        step = Step(len(self.steps) + 1, self.s.lens.stage, engineer, res.answer, q, opts, self.candidate,
                    list(notes or []) + res.notes, res)
        self.steps.append(step)
        return step

    def _reask(self, engineer: str, notes: list[str]) -> Step:
        """No model turn: record the action and ask again about the (new) candidate."""
        q, opts = self._ask()
        step = Step(len(self.steps) + 1, self.s.lens.stage, engineer, "", q, opts, self.candidate, notes)
        self.steps.append(step)
        return step

    def _excluded(self) -> set[str]:
        led = self.s.ledger
        return set(led.ids_of("RULED_OUT")) | set(led.ids_of("CONFIRMED")) | set(led.ids_of("CONTRIBUTING"))

    def _candidate(self, res: TurnResult | None) -> str | None:
        """What the next yes / no refers to (deterministic, explainable)."""
        stage = self.s.lens.stage
        excluded = self._excluded()
        if stage == st.DIFF_ANALYSIS:
            targets = self.r.target_changes(self.s.lens, self.s.ledger.render(self.s.count))
            hunks = self.r.suspect_hunks(self.s.lens, targets) if targets else []
            return next((h["change_id"] for h in hunks if h["change_id"] not in excluded), None)
        if stage not in st.MACRO_STAGES:
            return None
        if stage == st.SUSPECTS and self.s.lens.focus and self.s.lens.focus not in excluded:
            return self.s.lens.focus
        cands: list[str] = []
        if res is not None:
            cands += [i for i in cited_ids(res.answer) if i.startswith("evt_")]
            ctx = [i for i in res.context_ids if i.startswith("evt_")]
            evs = {e["id"]: e for e in self.r.store.get_events(ids=ctx)}
            cands += sorted((i for i in ctx if i in evs), key=lambda i: evs[i]["occurred_at"], reverse=True)
        cands += [e["id"] for e in self.r.ranked_changes(self.s.lens, "what changed before the errors?")[:10]]
        changes = {e["id"] for e in self.r.store.get_events(ids=list(dict.fromkeys(cands)))
                   if e["event_type"] in config.CHANGE_TYPES}
        return next((i for i in dict.fromkeys(cands) if i in changes and i not in excluded), None)

    def _describe(self, eid: str) -> str:
        if eid.startswith("chg_"):
            c = self.r.store.get_change_detail(eid)
            return f"{c['file']}:{c['line_start']}" if c else eid
        ev = self.r.store.get_event(eid)
        if not ev:
            return eid
        return f"{ev['event_type']} {ev['service']} {ev.get('version_from') or ''}->{ev.get('version_to') or ''}"

    def _ask(self) -> tuple[str, list[Option]]:
        """The question back to the engineer, and the one-click answers."""
        stage, c = self.s.lens.stage, self.candidate
        hops = list(config.dependencies(self.s.lens.service))
        if config.host_of(self.s.lens.service):
            hops.append(config.host_of(self.s.lens.service))
        follow = [Option(f"Check {h}", f"lens {h}") for h in hops[:2]]
        d = self._describe(c) if c else ""
        if stage == st.CHANGE_TIMELINE:
            if c:
                return (f"Should we examine [{c}] ({d})? Reply yes, another id, 'lens <service>' to check a dependency, "
                        f"or ask anything.", [Option(f"Yes, examine {c}", "yes"), Option("No, next candidate", "no"),
                                              *follow, Option("Runtime evidence", "stage runtime")])
            return ("No change stands out here. Check a dependency, look at the runtime evidence, or ask.",
                    [*follow, Option("Runtime evidence", "stage runtime")])
        if stage in (st.SUSPECTS, st.RUNTIME, st.HYPOTHESIS, st.SCOPE):
            if c:
                return (f"Is [{c}] ({d}) the root cause? 'yes' confirms it and opens its diff, 'no' rules it out.",
                        [Option(f"Yes, {c} is the root cause", "yes"), Option(f"No, rule out {c}", "no"),
                         *follow, Option("Next step", "next"), Option("No change explains it", "no change")])
            return ("No candidate left in this service. Check a dependency, or conclude.",
                    [*follow, Option("No change explains it", "no change")])
        if stage == st.DIFF_ANALYSIS:
            if c:
                return (f"Is [{c}] ({d}) the line that broke production? 'yes' confirms it, 'no' shows the next "
                        f"suspect line.", [Option(f"Yes, {c} is the breaking line", "yes"),
                                           Option("No, next suspect line", "no"), Option("Skip to impact", "next")])
            return ("No suspect line left in this change. Confirm another change id, or move on.",
                    [Option("Skip to impact", "next")])
        if stage == st.BLAST_RADIUS:
            return ("Anything to add about the impact? Otherwise, on to the fix.",
                    [Option("On to the fix", "next"), Option("Which service is hit worst?",
                                                             "Which service is hit worst and why?")])
        if stage == st.REMEDIATION:
            return ("Accept this fix and write the RCA report?", [Option("Yes, write the RCA report", "yes"),
                                                                   Option("Any alternative fix?",
                                                                          "What is an alternative fix?")])
        return ("The RCA report is ready.", [])
