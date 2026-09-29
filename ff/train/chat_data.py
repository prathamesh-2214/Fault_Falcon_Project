"""Conversational training data: engineer <-> FaultFalcon dialogues grounded in the incidents.

Each dialogue walks one incident the way an on-call engineer talks to an assistant: a casual
opener, "why that one?", a question about a harmless change, "check the dependency", "show me the
line", "explain it simply", "who else is hit?", "how do we fix it?", "thanks". Every FaultFalcon
reply is short (at most 3-4 sentences), cites only ids that appear in the context of that turn,
says so when the context does not answer a question, and usually ends with one question back.

The user turns carry the retrieved context the way the runtime does (``Context:`` + the
engineer's words), so a model trained on it learns to talk *from* evidence, not from memory.

Only ``dev`` incidents go into the training file; ``heldout`` incidents go into a separate
evaluation file (never train on them). Output (JSON lines, one dialogue per line)::

    data/training/conversations_train.jsonl   # 3 dialogue styles per dev incident
    data/training/conversations_eval.jsonl    # 1 per held-out incident
    data/training/README.md                   # data card

    python -m ff.train.chat_data
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from ff import config
from ff.config import get_paths

SYSTEM = config.SYSTEM_PROMPT + " Be brief and friendly; end with one question when a decision is needed."


def pick(rng: random.Random, *options: str) -> str:
    return rng.choice(options)


def _lag(start: str, t: str) -> str:
    h = (datetime.fromisoformat(start) - datetime.fromisoformat(t)).total_seconds() / 3600
    return f"{h * 60:.0f} minutes" if h < 1 else (f"{h:.1f} hours" if h < 48 else f"{h / 24:.0f} days")


def best_template(inc: dict[str, Any]) -> str:
    """The most informative error template of the incident (most words, fewest placeholders)."""
    import re

    tpls = inc.get("error_templates") or [""]
    return max(tpls, key=lambda t: len(re.findall(r"[A-Za-z]{4,}", t)) - 2 * t.count("<*>"))[:100]


def _diff_lines(diff: str) -> tuple[str, str]:
    rem = [ln[1:].strip() for ln in (diff or "").splitlines() if ln.startswith("-") and not ln.startswith("---")]
    add = [ln[1:].strip() for ln in (diff or "").splitlines() if ln.startswith("+") and not ln.startswith("+++")]
    return (rem[0] if rem else ""), (add[0] if add else "")


@dataclass
class Facts:
    """Everything a dialogue about one incident may say (all from the saved world)."""

    inc: dict[str, Any]
    anomaly: str  # compressed ANOMALY record
    cause: dict[str, Any] | None
    cause_rec: str
    what: str  # "what the cause changed", in words
    decoy_id: str | None
    decoy_rec: str
    path: list[str]  # alert -> ... -> cause component
    hunk_rec: str
    blast: list[str]
    extra: dict[str, Any] = field(default_factory=dict)


def facts_for(inc: dict[str, Any], ranker: Any) -> Facts:
    store = ranker.store
    ev = store.get_event(inc["anomaly_id"])
    anomaly = ranker.compress(ev) if ev else f"ANOMALY {inc['service']} at {inc['incident_start'][:16]}"
    cause = store.get_event(inc["cause_event_id"]) if inc.get("cause_event_id") else None
    cause_rec, what = "", ""
    if cause:
        cds = store.get_change_details([cause["id"]])
        cause_rec = ranker.compress(cause, cds)
        params = [f"{c['param_key']} {c['param_old']} -> {c['param_new']}" for c in cds if c.get("param_key")]
        files = sorted({c["file"] for c in cds if c.get("file") and not c.get("param_key")})
        what = "; ".join(params) or ", ".join(f.rsplit("/", 1)[-1] for f in files[:2]) or "a release"
    decoy_id = next(iter(inc.get("decoy_ids") or []), None)
    decoy = store.get_event(decoy_id) if decoy_id else None
    decoy_rec = ranker.compress(decoy) if decoy else ""
    hunk_rec = ""
    c = inc.get("culprit")
    if c:
        head = f"[{c['change_id']}] {c['file']}:{c['line']} {c.get('symbol') or ''} (in [{c['event_id']}])"
        hunk_rec = head + "\n" + "\n".join((c.get("diff") or "").splitlines()[:8])
    path = config.dependency_path(inc["service"], inc.get("cause_service") or inc["service"]) or [inc["service"]]
    return Facts(inc, anomaly, cause, cause_rec, what, decoy_id, decoy_rec, path, hunk_rec,
                 inc.get("blast_radius") or [inc["service"]])


# --------------------------------------------------------------------------- turns
Turn = tuple[str, str, str]  # (context, engineer, assistant)


def opener(f: Facts, rng: random.Random) -> Turn:
    inc, n = f.inc, f.inc.get("anomaly_numbers") or {}
    t = inc["incident_start"][11:16]
    user = pick(rng, f"hey, {inc['service']} is paging: {inc['title'].lower()}. what changed?",
                f"we got \"{inc['detection']['alert']}\" at {t}. can you help me figure out why?",
                f"on-call here. {inc['service']} error rate jumped around {t}. any idea what's going on?",
                f"{inc['service']} looks broken since {t}, where do I start?")
    ctx = [f.anomaly]
    symptom = (f"{inc['service']} went from an error ratio of {n.get('baseline_median_error_ratio', 0):.2f} to "
               f"{n.get('peak_error_ratio', 0):.2f} at {t} [{inc['anomaly_id']}].")
    if f.cause and len(f.path) <= 2 and f.cause_rec:  # the cause is in view from the alerting component
        ctx.append(f.cause_rec)
        if f.decoy_rec:
            ctx.append(f.decoy_rec)
        lag = _lag(inc["incident_start"], f.cause["occurred_at"])
        reply = (f"{symptom} The change that fits best is [{f.cause['id']}] on {f.cause['service']} ({f.what}), "
                 f"{lag} before it started. " + pick(rng, "Want me to dig into it?", "Shall we look at it closer?",
                                                     "Should I examine it?"))
        return "\n".join(ctx), user, reply
    if f.cause:
        hop = f.path[1] if len(f.path) > 1 else f.inc.get("cause_service")
        if f.decoy_rec:
            ctx.append(f.decoy_rec)
        why = f" [{f.decoy_id}] is the only recent change here and it does not match the errors." if f.decoy_id else ""
        reply = (f"{symptom} Nothing changed in {inc['service']} that explains it.{why} "
                 f"{relation(inc['service'], hop).replace('it ', inc['service'] + ' ', 1)}, so the fault may come "
                 f"from there. " + pick(rng, f"Shall I check {hop}?",
                                                                    f"Want me to look at {hop}'s changes?"))
        return "\n".join(ctx), user, reply
    reply = (f"{symptom} I don't see a change before it that fits: no deploy, config or Terraform change lines up "
             f"with these errors. " + pick(rng, "Want me to look at the runtime evidence instead?",
                                           "Should we look at the error signatures?"))
    return "\n".join(ctx), user, reply


def relation(a: str, b: str) -> str:
    """How component ``a`` depends on ``b``, in words."""
    if b in config.QUEUE_PRODUCERS.get(a, ()):
        return f"it is fed by {b}, which publishes into it"
    if config.host_of(a) == b:
        return f"it runs on {b}"
    return f"it calls {b}"


def follow_hops(f: Facts, rng: random.Random) -> list[Turn]:
    """One turn per dependency hop until the cause is in view."""
    out = []
    for k, hop in enumerate(f.path[1:], 1):
        last = k == len(f.path) - 1
        user = pick(rng, f"ok, check {hop}", "yes, go one level down", f"sure, what changed in {hop}?")
        if not last:
            nxt = f.path[k + 1]
            out.append(("", user, f"{hop} itself shows nothing suspicious, but {relation(hop, nxt)}. "
                                  + pick(rng, f"Next hop: {nxt}?", f"Shall I continue to {nxt}?")))
            continue
        lag = _lag(f.inc["incident_start"], f.cause["occurred_at"])
        out.append((f.cause_rec, user,
                    f"Found it: [{f.cause['id']}] on {hop} ({f.what}), {lag} before the errors started in "
                    f"{f.inc['service']}. " + pick(rng, "Want me to open its diff?", "Should I confirm it and drill in?")))
    return out


def why_that_one(f: Facts, rng: random.Random) -> Turn:
    tpl = best_template(f.inc)
    culprit = (f.inc.get("culprit") or {}).get("file", "").rsplit("/", 1)[-1]
    touches = f" (including {culprit})" if culprit and culprit not in f.what else ""
    reply = (f"Timing and content: [{f.cause['id']}] landed {_lag(f.inc['incident_start'], f.cause['occurred_at'])} "
             f"before the errors and it is the change on the dependency path that touches what fails: "
             f"{f.what}{touches}. The errors (`{tpl}`) start right after it. "
             + pick(rng, "Shall I show the exact line?", "Want to see the diff?"))
    return f.cause_rec, pick(rng, "why that one?", "how sure are you?", "what makes you think it's that change?"), reply


def about_decoy(f: Facts, rng: random.Random) -> Turn:
    reply = (f"[{f.decoy_id}] is closer in time, but " + pick(rng, "it only touched harmless settings or docs",
                                                            "what it changed has nothing to do with these errors")
             + f"; it doesn't explain the error signature. [{f.cause['id']}] still fits best. Keep going with it?")
    return f.decoy_rec, pick(rng, f"what about [{f.decoy_id}]? it's more recent", f"could it be [{f.decoy_id}] instead?"), reply


def show_line(f: Facts, rng: random.Random) -> Turn:
    c = f.inc["culprit"]
    old, new = _diff_lines(c.get("diff") or "")
    change = f"`{old}` became `{new}`" if old and new else (f"it adds `{new}`" if new else f"it removes `{old}`")
    user = pick(rng, "yes, show me the line", "which line broke it?", "ok, drill in", "go ahead")
    reply = (f"The breaking line is [{c['change_id']}] in {c['file']}:{c['line']}: {change}. "
             + pick(rng, "Is that the one we confirm?", "Confirm it as the breaking line?"))
    return f.hunk_rec, user, reply


def explain_simply(f: Facts, rng: random.Random) -> Turn:
    macro = (f.inc.get("rca") or {}).get("macro", "")
    first = macro.split(";")[0].split(". ")[0].rstrip(".")
    return ("", pick(rng, "explain it like I'm new here", "can you say that in plain words?", "tldr?"),
            f"In short: {first}. " + pick(rng, "Want to see who else is affected?", "Shall we check the impact?"))


def blast(f: Facts, rng: random.Random) -> Turn:
    source = f.inc.get("cause_service") or f.inc["service"]
    others = [b for b in f.blast if b != source]
    ctx = "Topology: " + "; ".join(f"{s} calls {', '.join(config.dependencies(s))}" for s in f.blast
                                   if config.dependencies(s))
    reply = (f"Affected: {', '.join(f.blast)}. The fault starts in {source} and "
             f"reaches {', '.join(others) or 'nothing else'} through their calls. " + pick(rng, "On to the fix?",
                                                                                          "Shall we talk about the fix?"))
    return ctx, pick(rng, "who else is hit?", "is anything else affected?", "blast radius?"), reply


def fix(f: Facts, rng: random.Random) -> Turn:
    rem = (f.inc.get("rca") or {}).get("remediation", "")
    items = f.inc.get("action_items") or []
    follow = f" Follow-up: {items[0]}" if items else ""
    user = pick(rng, "how do we fix it?", "what should we do now?", "fix?")
    reply = f"{rem}{follow} " + pick(rng, "Want me to write the RCA report?", "Shall I close this out with a report?")
    return "", user, reply


def unknown(f: Facts, rng: random.Random) -> Turn:
    related = {"CERT_EXPIRY": "a certificate", "TRAFFIC_SPIKE": "a traffic spike", "RDS_MAINTENANCE": "DNS",
               "LB_HEALTHCHECK": "the load balancer", "ALB_IDLE_TIMEOUT": "the load balancer"}
    things = [t for t in ("DNS", "an expired certificate", "a traffic spike", "a clock skew between hosts")
              if t != related.get(f.inc["scenario_type"])]
    thing = rng.choice(things)
    return ("", f"could {thing} be involved?",
            f"Nothing in the context points at {thing}: the records I have are the changes and the error signatures "
            f"above. " + pick(rng, "Should I stay with the current lead?", "Want me to look for evidence of it?"))


def closing(f: Facts, rng: random.Random) -> Turn:
    if f.cause:
        c = f.inc.get("culprit") or {}
        text = (f"Done. Root cause: [{f.cause['id']}] ({f.what}); breaking line: [{c.get('change_id')}] "
                f"{c.get('file')}:{c.get('line')}. The RCA report is ready in the RCA tab.")
    else:
        text = (f"Done. No change caused it: {f.inc.get('external_factor') or 'an external factor'}. The RCA report "
                f"records it as an external cause.")
    return "", pick(rng, "yes please, thanks!", "great, write it up", "thanks, that's it"), text


def external_turns(f: Facts, rng: random.Random) -> list[Turn]:
    tpl = best_template(f.inc)
    ext = f.inc.get("external_factor") or "an external factor"
    how = (f.inc.get("rca") or {}).get("remediation", "")
    return [(f.anomaly, pick(rng, "yes, show me the errors", "what do the logs say?"),
             f"The errors are `{tpl}`. That points outside our changes: {ext}. "
             + pick(rng, "Want the mitigation?", "Shall we handle it as an external issue?")),
            ("", "what do we do?", f"{how} " + pick(rng, "Want me to write it up?", "Shall I close it out?"))]


def dialogue(f: Facts, rng: random.Random, style: str) -> list[Turn]:
    """The turns of one dialogue. Styles: 'direct' (straight to the fix), 'skeptic' (pushes back,
    asks about the decoy), 'learner' (asks for plain words, adds an off-topic question)."""
    turns = [opener(f, rng)]
    if not f.cause:
        return turns + external_turns(f, rng) + [closing(f, rng)]
    if len(f.path) > 2:  # the cause is out of view from the alert: follow the chain
        turns += follow_hops(f, rng)
    if style == "skeptic":
        turns.append(why_that_one(f, rng))
        if f.decoy_id and f.decoy_rec:
            turns.append(about_decoy(f, rng))
    if f.inc.get("culprit"):
        turns.append(show_line(f, rng))
    if style == "learner":
        turns.append(explain_simply(f, rng))
        turns.append(unknown(f, rng))
    if style != "direct" or rng.random() < 0.5:
        turns.append(blast(f, rng))
    turns.append(fix(f, rng))
    turns.append(closing(f, rng))
    return turns


def to_messages(turns: list[Turn]) -> list[dict[str, str]]:
    msgs = [{"role": "system", "content": SYSTEM}]
    for ctx, user, reply in turns:
        content = f"Context:\n{ctx}\nEngineer: {user}" if ctx else user
        msgs += [{"role": "user", "content": content}, {"role": "assistant", "content": reply}]
    return msgs


def grounded(messages: list[dict[str, str]]) -> bool:
    """Every id an assistant turn cites appears in an earlier message of the dialogue."""
    import re

    seen = ""
    for m in messages:
        if m["role"] == "assistant":
            ids = set(re.findall(r"\[((?:evt|chg)_\d+)\]", m["content"]))
            if any(i not in seen for i in ids):
                return False
        seen += m["content"]
    return True


def build(paths: Any = None, seed: int = config.SEED, styles: tuple[str, ...] = ("direct", "skeptic", "learner"),
          world: Any = None, ranker: Any = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(train, eval) dialogues from the saved world (or ``world`` + ``ranker`` given directly)."""
    from ff.ingest.pipeline import open_stores
    from ff.ingest.simulator import load_world
    from ff.retrieve.ranker import Ranker

    paths = paths or get_paths()
    world = world or load_world(paths)
    ranker = ranker or Ranker(*open_stores(paths))
    rng = random.Random(seed + 23)
    train, evals = [], []
    for inc in world.incidents:
        f = facts_for(inc, ranker)
        use = styles if inc["split"] == "dev" else (rng.choice(styles),)
        for style in use:
            msgs = to_messages(dialogue(f, rng, style))
            if not grounded(msgs):
                continue
            row = {"id": f"{inc['id']}-{style}", "incident": inc["id"], "split": inc["split"], "style": style,
                   "scenario": inc["scenario_type"], "category": inc.get("category"), "messages": msgs}
            (train if inc["split"] == "dev" else evals).append(row)
    return train, evals


CARD = """# Conversational training data

Engineer <-> FaultFalcon dialogues about the incidents in `data/incidents/`, for fine-tuning the model to
talk like an on-call assistant: short answers grounded in the context of each turn, one question back when a
decision is needed, honest "the context does not show that" answers, pushback handled with evidence.

| File | Dialogues | Incidents |
| --- | --- | --- |
| `conversations_train.jsonl` | {n_train} | {n_train_inc} `dev` incidents x 3 styles (direct, skeptic, learner) |
| `conversations_eval.jsonl` | {n_eval} | {n_eval_inc} `heldout` incidents (never train on these) |

One JSON object per line: `id`, `incident`, `split`, `style`, `scenario`, `category`, `messages` (a list of
`{{"role": "system" | "user" | "assistant", "content": ...}}`). User turns that need evidence start with
`Context:` (the records the retriever would show) followed by `Engineer: ...`. Every id an assistant turn
cites appears earlier in the same dialogue (checked when the file is written).

Turn types: opener (symptom + best candidate or the next dependency to check), dependency hops, "why that
one?", a question about a harmless recent change, the breaking line, a plain-words explanation, an off-topic
question the context cannot answer, blast radius, the fix with a follow-up, closing summary. External
incidents end with "no change caused it" and the external factor.

Generated by `python -m ff.train.chat_data` from the saved dataset (seeded, reproducible). {n_turns} assistant
turns in the training file, {avg:.1f} per dialogue.
"""


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=None, help="output folder (default: data/training)")
    args = ap.parse_args(argv)
    paths = get_paths()
    out = Path(args.out) if args.out else paths.data / "training"
    out.mkdir(parents=True, exist_ok=True)
    train, evals = build(paths)
    for name, rows in (("conversations_train.jsonl", train), ("conversations_eval.jsonl", evals)):
        with open(out / name, "w", encoding="utf-8") as fh:
            for r in rows:
                fh.write(json.dumps(r) + "\n")
    n_turns = sum(sum(m["role"] == "assistant" for m in r["messages"]) for r in train)
    (out / "README.md").write_text(CARD.format(
        n_train=len(train), n_train_inc=len({r["incident"] for r in train}), n_eval=len(evals),
        n_eval_inc=len({r["incident"] for r in evals}), n_turns=n_turns,
        avg=n_turns / max(1, len(train))), encoding="utf-8")
    print(f"wrote {len(train)} training and {len(evals)} evaluation dialogues ({n_turns} assistant turns) to {out}")


if __name__ == "__main__":
    main()
