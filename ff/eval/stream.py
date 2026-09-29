"""The token stream used by the smoke check and Experiment A.

All ANOMALY / ERROR_SIGNATURE narratives and deployment records in timeline order, then a
scripted session transcript. Built from the saved world, so it needs no model.
"""

from __future__ import annotations

from pathlib import Path

from ff.config import Paths, get_paths

TRANSCRIPT = """Engineer: compute-svc error burst since 08:40, what changed?
FaultFalcon: The last prod deploy of compute-svc happened before the burst, and a storage-svc deploy landed 20 minutes earlier. Checking changes before symptoms.
Engineer: Rule out the dashboard-only deploy, it only touched metrics/dashboards.json.
FaultFalcon: Ruled out. The remaining suspect changed RMClient.allocate and the error signature is 'ERROR IN CONTACTING RM'.
Engineer: What did the tests say for that release?
FaultFalcon: The ppd load suite was skipped; unit and integration suites passed.
"""


def build_stream_text(paths: Paths | None = None, repeat_transcript: int = 20) -> str:
    """Narratives of every event (logs + simulated) in time order, then the transcript."""
    from ff.ingest.deploy_summarizer import summarize_all
    from ff.ingest.simulator import load_world

    paths = paths or get_paths()
    world = load_world(paths)
    events = summarize_all(world.events, world.change_details)
    events.sort(key=lambda e: (e["occurred_at"], int(e["id"].split("_")[1])))
    body = "\n\n".join(e["narrative"] for e in events if e.get("narrative"))
    return body + "\n\n" + "\n".join([TRANSCRIPT] * repeat_transcript)


def build_stream_ids(tok, n_tokens: int, paths: Paths | None = None, save_to: Path | None = None) -> list[int]:
    """First ``n_tokens`` token ids of the stream (BOS first); optionally saves the text used."""
    text = build_stream_text(paths)
    ids = list(tok.encode(text, add_special_tokens=True))
    while len(ids) < n_tokens:  # small worlds (fixtures): repeat the body
        text = text + "\n\n" + text
        ids = list(tok.encode(text, add_special_tokens=True))
    ids = ids[:n_tokens]
    if save_to is not None:
        save_to.parent.mkdir(parents=True, exist_ok=True)
        save_to.write_text(tok.decode(ids, skip_special_tokens=True), encoding="utf-8")
    return ids
