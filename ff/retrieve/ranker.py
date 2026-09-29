"""Stage-aware retrieval and ranking under a small token budget (TinyLlama's rolling window).

``retrieve()``:
1. Hard filters: service in lens service + direct dependencies + host; time in
   [incident_start - window_days, incident_start + 2 h].
2. Stage filter by event type (SCOPE: none; CHANGE_TIMELINE: changes; SUSPECTS: focus
   event + its TEST_RUN / SECURITY_SCAN; RUNTIME: ANOMALY / ERROR_SIGNATURE (+ docs);
   HYPOTHESIS / CLOSE: ids cited in the ledger + the top ANOMALY).
3. Candidates: top 25 by similarity UNION exact matches (error templates named in the
   question, file / symbol names from change_detail).
4. score = 0.45 sim + 0.25 exp(-dt / tau) + 0.20 significance / 5 + 0.10 keyword.
5. Guaranteed changes: the 2 most recent significance>=4 changes of the lens service plus
   its most recent significance-5 change (so a latent infra change from 15 days ago
   survives), and the most recent one of each dependency / host.
6. Greedy packing into slots (guaranteed 35% / scored 45% / architecture 20%).
7. Chronological order, one structural summary line per change.
Anchored ids (the engineer's checkpoints, already in the KV start region) are never
returned; their explanations are tagged ``anchored``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from ff import config
from ff.engine import stages as st
from ff.engine.lens import Lens
from ff.store.sqlite_store import SqliteStore
from ff.store.vector_store import VectorStore, approx_tokens

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9_.\-]{3,}")
_IDS = re.compile(r"\b((?:evt|chg)_\d+)\b")
STOP = {"what", "which", "that", "this", "with", "from", "have", "were", "when", "since", "there", "about",
        "changed", "change", "could", "would", "should", "does", "into", "after", "before", "their", "them",
        "error", "errors", "service", "incident"}


@dataclass
class RetrievalResult:
    """Context for one turn."""

    context_text: str
    ids: list[str]
    explanations: list[dict[str, Any]] = field(default_factory=list)
    tokens: int = 0
    arch_ids: list[str] = field(default_factory=list)
    top_change_score: float | None = None


@dataclass
class RankerFlags:
    """Ablation switches (all on = FaultFalcon)."""

    guarantees: bool = True
    architecture: bool = True
    stage_filter: bool = True


def _dt(x: str) -> datetime:
    return datetime.fromisoformat(x)


# which team knowledge each stage consults (see Ranker.knowledge)
KNOWLEDGE_KINDS: dict[str, tuple[str, ...]] = {
    st.SCOPE: ("topology", "patterns"), st.CHANGE_TIMELINE: ("release", "changelog"),
    st.SUSPECTS: ("codebase", "changelog"), st.RUNTIME: ("runbook", "postmortem"),
    st.HYPOTHESIS: ("postmortem", "runbook"), st.DIFF_ANALYSIS: ("codebase",),
    st.BLAST_RADIUS: ("topology", "patterns"), st.REMEDIATION: ("postmortem", "runbook"), st.CLOSE: ("postmortem",),
}


def _test_notes(payload: dict[str, Any]) -> list[str]:
    """'ppd load tests skipped' / 'prod e2e tests failed' from the release's test runs."""
    rel = payload.get("release_tests") or {}
    if not rel and payload.get("tests"):
        rel = {"": payload["tests"]}
    notes = []
    for env, t in rel.items():
        prefix = f"{env} " if env else ""
        if t.get("skipped_suites"):
            notes.append(f"{prefix}{', '.join(t['skipped_suites'])} tests skipped")
        if t.get("failed_suites"):
            notes.append(f"{prefix}{', '.join(t['failed_suites'])} tests failed")
    return notes


def keywords(text: str) -> set[str]:
    return {w.lower().strip(".-") for w in _WORD.findall(text)} - STOP


class Ranker:
    """Retrieval over a SqliteStore + VectorStore."""

    def __init__(self, store: SqliteStore, vectors: VectorStore, count: Callable[[str], int] | None = None,
                 flags: RankerFlags | None = None) -> None:
        self.store = store
        self.vectors = vectors
        self.count = count or approx_tokens
        self.flags = flags or RankerFlags()

    # ------------------------------------------------------------------ helpers
    def _truncate(self, text: str, max_tokens: int) -> str:
        if self.count(text) <= max_tokens:
            return text
        words = text.split()
        lo, hi = 0, len(words)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.count(" ".join(words[:mid]) + " ...") <= max_tokens:
                lo = mid
            else:
                hi = mid - 1
        return " ".join(words[:lo]) + " ..."

    def structural_summary(self, ev: dict[str, Any], cds: list[dict[str, Any]]) -> str:
        """'2 files, 18 lines; http.timeout_ms 2000 -> 500; load skipped; post-deploy err 0.00->0.16'."""
        parts = []
        files = {c["file"] for c in cds if c.get("file")}
        lines = sum(int(c.get("lines_changed") or 0) for c in cds)
        if files:
            parts.append(f"{len(files)} file{'s' if len(files) > 1 else ''}, {lines} lines ({', '.join(sorted(files))[:90]})")
        for c in cds:
            if c.get("param_key"):
                parts.append(f"{c['param_key']} {c['param_old']} -> {c['param_new']}")
        p = ev.get("payload") or {}
        parts += _test_notes(p)
        pd_line = p.get("post_deploy") or ""
        m = re.match(r"error ratio ([\d.]+).*-> ([\d.]+)", pd_line)
        if m:
            parts.append(f"post-deploy error ratio {m.group(1)} -> {m.group(2)}")
        if p.get("partial"):
            parts.append("partial rollback")
        return "; ".join(parts)

    def render_record(self, ev: dict[str, Any], cds: list[dict[str, Any]], max_tokens: int = 90) -> str:
        """First 2 narrative lines with the id, plus the structural summary for changes."""
        lines = [ln for ln in (ev.get("narrative") or "").splitlines() if ln.strip()]
        head = re.sub(r"^\[[A-Z_]+\]\s*", "", lines[0]) if lines else ""
        text = f"[{ev['id']}] {ev['event_type']} {head}"
        if ev["event_type"] in config.CHANGE_TYPES:  # structural facts first: they survive truncation
            s = self.structural_summary(ev, cds)
            if s:
                text += " | " + s
        if len(lines) > 1 and not lines[1].startswith("Structural"):
            text += " | " + lines[1]
        return self._truncate(text, max_tokens)

    def compress(self, ev: dict[str, Any], cds: list[dict[str, Any]] | None = None) -> str:
        """<= 60-token form for the anchor block, e.g.
        '[evt_12] DEPLOY compute-svc v3.4->v3.5 prod 08-29 08:36; http.timeout_ms 2000->500; load tests skipped'."""
        cds = self.store.get_change_details([ev["id"]]) if cds is None else cds
        t = _dt(ev["occurred_at"]).strftime("%m-%d %H:%M")
        p = ev.get("payload") or {}
        et = ev["event_type"]
        if et in config.CHANGE_TYPES:
            head = f"[{ev['id']}] {et} {ev['service']} {ev.get('version_from') or ''}->{ev.get('version_to') or ''} " \
                   f"{ev.get('environment', 'prod')} {t}"
            extra = [f"{c['param_key']} {c['param_old']}->{c['param_new']}" for c in cds if c.get("param_key")]
            files = sorted({c["file"] for c in cds if c.get("file") and not c.get("param_key")})
            if files and not extra:
                extra.append(", ".join(files[:2]))
            extra += _test_notes(p)
            if p.get("partial"):
                extra.append("partial")
            text = "; ".join([head, *extra])
        elif et == "ANOMALY":
            end = _dt(ev["end_at"]).strftime("%H:%M") if ev.get("end_at") else ""
            tpl = (p.get("templates") or [""])[0]
            text = (f"[{ev['id']}] ANOMALY {ev['service']} {t}-{end} error ratio {p.get('peak_error_ratio', 0):.2f} "
                    f"vs {p.get('baseline_median_error_ratio', 0):.2f}; {tpl}")
        elif et == "ERROR_SIGNATURE":
            text = f"[{ev['id']}] ERROR_SIGNATURE {ev['service']} {t} `{p.get('template', '')}` x{p.get('count', 0)}"
        elif et == "TEST_RUN":
            text = (f"[{ev['id']}] TEST_RUN {ev['service']} {ev.get('version_to') or ''} {ev.get('environment')} {t}: "
                    f"{p.get('failed', 0)} failed, {p.get('skipped', 0)} skipped")
        else:
            text = f"[{ev['id']}] {et} {ev['service']} {t}"
        return self._truncate(text, config.COMPRESS_MAX_TOKENS)

    def compress_id(self, event_id: str) -> str:
        """:meth:`compress` by id (``[evt_x] (unknown event)`` if missing)."""
        ev = self.store.get_event(event_id)
        return self.compress(ev) if ev else f"[{event_id}] (unknown event)"

    def baseline_text(self, service: str, environment: str = "prod") -> str:
        """Compressed baseline (real log statistics) for the lens_baseline segment."""
        b = self.store.get_baseline(service, environment)
        if not b:
            return f"Baseline for {service}: none recorded."
        band = b.get("error_ratio_band", [0, 0])
        text = (f"Baseline for {service} from logs: error ratio {band[0]:.2f}-{band[1]:.2f} "
                f"(median {b.get('error_ratio_median', 0):.2f})")
        if b.get("p95_latency_band"):
            lb = b["p95_latency_band"]
            text += f", p95 latency {lb[0]:.2f}-{lb[1]:.2f} s"
        if b.get("known_noise"):
            text += f"; known noise: {b['known_noise'][0][:60]}"
        return text + "."

    # ------------------------------------------------------------------ candidate pools
    def pool(self, lens: Lens) -> list[dict[str, Any]]:
        """Hard filters: neighbourhood services and the time window."""
        return self.store.get_events(services=config.neighbourhood(lens.service),
                                     start=lens.window_start.isoformat(), end=lens.window_end.isoformat())

    def guaranteed(self, lens: Lens, events: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
        """Major changes kept regardless of score (see module docstring)."""
        before = [e for e in events if e["event_type"] in config.CHANGE_TYPES and e.get("environment") == lens.environment
                  and e["significance"] >= config.GUARANTEE_MIN_SIG and _dt(e["occurred_at"]) <= lens.incident_start]
        own = sorted((e for e in before if e["service"] == lens.service), key=lambda e: e["occurred_at"], reverse=True)
        out = own[:config.GUARANTEE_LENS - 1]
        # the latest change to each infra resource: latent causes (a disk shrink 15 days ago) survive
        seen_res: set[str] = set()
        infra = [e for e in own if e["event_type"] == "INFRA_CHANGE"]  # most recent first
        cds_by_event: dict[str, list[dict[str, Any]]] = {}
        for c in self.store.get_change_details([e["id"] for e in infra]):
            cds_by_event.setdefault(c["event_id"], []).append(c)
        for ev in infra:
            keys = {c.get("param_key") or c.get("infra_resource") or "" for c in cds_by_event.get(ev["id"], [])}
            if keys - seen_res and ev not in out:
                out.append(ev)
            seen_res |= keys
        for svc in config.neighbourhood(lens.service)[1:]:
            other = sorted((e for e in before if e["service"] == svc), key=lambda e: e["occurred_at"], reverse=True)
            out += other[:config.GUARANTEE_NEIGHBOUR]
        return out

    def stage_filter(self, lens: Lens, stage: str, events: list[dict[str, Any]], ledger_text: str,
                     top_changes: Callable[[], list[dict[str, Any]]]) -> list[dict[str, Any]]:
        if not self.flags.stage_filter:
            return events
        if stage == st.SCOPE:
            return []
        if stage == st.CHANGE_TIMELINE:
            return [e for e in events if e["event_type"] in config.CHANGE_TYPES
                    and e.get("environment") == lens.environment]
        if stage == st.RUNTIME:
            return [e for e in events if e["event_type"] in config.RUNTIME_TYPES]
        by_id = {e["id"]: e for e in events}
        if stage == st.SUSPECTS:
            focus = [by_id[lens.focus]] if lens.focus in by_id else top_changes()[:2]
            out = list(focus)
            for f in focus:
                p = f.get("payload") or {}
                for k in ("test_run_id", "scan_id"):
                    if p.get(k) in by_id:
                        out.append(by_id[p[k]])
                # every stage's TEST_RUN of the same release (a skipped ppd suite shows up here)
                out += [e for e in events if e["event_type"] == "TEST_RUN" and e["service"] == f["service"]
                        and e.get("version_to") == f.get("version_to") and e not in out]
            return out
        # HYPOTHESIS / CLOSE
        cited = [by_id[i] for i in _IDS.findall(ledger_text) if i in by_id]
        anomalies = sorted((e for e in events if e["event_type"] == "ANOMALY" and e["service"] == lens.service),
                           key=lambda e: (abs((_dt(e["occurred_at"]) - lens.incident_start).total_seconds()),
                                          -e["significance"]))
        return cited + anomalies[:1]

    def exact_matches(self, question: str, events: Sequence[dict[str, Any]],
                      cds_by_event: dict[str, list[dict[str, Any]]]) -> set[str]:
        """Error templates named in the question, and file/symbol names from change_detail."""
        q = question.lower()
        qk = keywords(question)
        out = set()
        for e in events:
            if e["event_type"] == "ERROR_SIGNATURE":
                tk = keywords((e.get("payload") or {}).get("template", ""))
                if tk and len(tk & qk) >= min(2, len(tk)):
                    out.add(e["id"])
            for c in cds_by_event.get(e["id"], []):
                names = [c.get("file") or "", (c.get("file") or "").split("/")[-1], c.get("symbol") or "",
                         c.get("param_key") or ""]
                if any(n and len(n) > 3 and n.lower() in q for n in names):
                    out.add(e["id"])
        return out

    def score(self, ev: dict[str, Any], sim: float, lens: Lens, qk: set[str]) -> dict[str, float]:
        w = config.RANKER_WEIGHTS
        dt = abs((lens.incident_start - _dt(ev["occurred_at"])).total_seconds()) / 86400
        tau = config.RECENCY_TAU_DAYS.get(ev["event_type"], 14.0)
        recency = math.exp(-dt / tau)
        kw = len(qk & keywords(ev.get("narrative") or "")) / len(qk) if qk else 0.0
        total = w["sim"] * sim + w["recency"] * recency + w["significance"] * ev["significance"] / 5 + w["keyword"] * kw
        return {"sim": sim, "recency": recency, "keyword": kw, "score": total}

    # ------------------------------------------------------------------ retrieve
    def retrieve(self, lens: Lens, question: str, ledger_text: str = "", anchored_ids: Iterable[str] = (),
                 budget: int = config.RETRIEVAL_BUDGET, stage: str | None = None) -> RetrievalResult:
        """Stage-aware, lens-filtered retrieval packed into ``budget`` tokens."""
        stage = stage or lens.stage
        anchored = set(anchored_ids)
        if stage in st.MICRO_STAGES:
            return self.micro_retrieve(lens, question, ledger_text, anchored, budget, stage)
        pool = self.pool(lens)
        cds_by_event: dict[str, list[dict[str, Any]]] = {}
        for c in self.store.get_change_details([e["id"] for e in pool]):
            cds_by_event.setdefault(c["event_id"], []).append(c)
        query = f"{question} {lens.service}"
        sims = self.vectors.similarity(query, [e["id"] for e in pool])
        qk = keywords(question)
        scores = {e["id"]: self.score(e, sims.get(e["id"], 0.0), lens, qk) for e in pool}

        def top_changes() -> list[dict[str, Any]]:
            ch = [e for e in pool if e["event_type"] in config.CHANGE_TYPES and e["id"] not in anchored
                  and _dt(e["occurred_at"]) <= lens.incident_start]
            return sorted(ch, key=lambda e: -scores[e["id"]]["score"])

        staged = self.stage_filter(lens, stage, pool, ledger_text, top_changes)
        staged_ids = {e["id"] for e in staged}
        by_sim = sorted(staged, key=lambda e: -scores[e["id"]]["sim"])[:config.TOP_K_SIM]
        exact = self.exact_matches(question, staged, cds_by_event)
        cands = {e["id"]: e for e in by_sim}
        cands.update({e["id"]: e for e in staged if e["id"] in exact})
        if stage in (st.SUSPECTS, st.HYPOTHESIS, st.CLOSE):
            cands.update({e["id"]: e for e in staged})  # small, explicitly chosen sets
        guaranteed = []
        if self.flags.guarantees and stage in (st.CHANGE_TIMELINE, st.HYPOTHESIS, st.CLOSE):
            guaranteed = self.guaranteed(lens, pool)
        g_ids = {e["id"] for e in guaranteed}

        use_arch = self.flags.architecture and stage in KNOWLEDGE_KINDS
        shares = dict(config.SLOT_SHARES)
        if not use_arch:
            shares["scored"] += shares.pop("architecture")
            shares["architecture"] = 0.0
        slot_g = int(budget * shares["guaranteed"])
        slot_a = int(budget * shares["architecture"])

        chosen: list[tuple[dict[str, Any], str, bool]] = []
        used = 0
        explanations: list[dict[str, Any]] = []

        def explain(e: dict[str, Any], tag: str) -> dict[str, Any]:
            s = scores.get(e["id"], {"sim": 0.0, "recency": 0.0, "score": 0.0})
            return {"id": e["id"], "event_type": e["event_type"], "service": e["service"], "sim": round(s["sim"], 3),
                    "recency": round(s["recency"], 3), "significance": e["significance"],
                    "score": round(s["score"], 3), "guaranteed": e["id"] in g_ids, "source": e["source"], "tag": tag}

        for e in guaranteed + [c for c in cands.values() if c["id"] not in g_ids]:
            if e["id"] in anchored:
                explanations.append(explain(e, "anchored"))
        g_used = 0
        for e in sorted(guaranteed, key=lambda e: -scores[e["id"]]["score"]):
            if e["id"] in anchored:
                continue
            rec = self.compress(e, cds_by_event.get(e["id"], []))
            n = self.count(rec) + 1
            if g_used + n <= slot_g and used + n <= budget:
                chosen.append((e, rec, True))
                g_used += n
                used += n
        scored_budget = budget - slot_a
        for e in sorted(cands.values(), key=lambda e: -scores[e["id"]]["score"]):
            if e["id"] in anchored or any(c[0]["id"] == e["id"] for c in chosen):
                continue
            rec = self.render_record(e, cds_by_event.get(e["id"], []))
            n = self.count(rec) + 1
            if used + n <= scored_budget:
                chosen.append((e, rec, False))
                used += n
        arch_texts, arch_ids = [], []
        if use_arch:
            for ch in self.knowledge(lens, question, stage, n=2):
                text = self._truncate(f"[{ch['kind']}] {ch['text']}", max(20, slot_a))
                n = self.count(text) + 1
                if used + n <= budget:
                    arch_texts.append(text)
                    arch_ids.append(ch["id"])
                    used += n
        chosen.sort(key=lambda c: (c[0]["occurred_at"], c[0]["id"]))
        for e, _, _ in chosen:
            explanations.append(explain(e, "guaranteed" if e["id"] in g_ids else "scored"))
        context = "\n".join([rec for _, rec, _ in chosen] + arch_texts)
        tops = [scores[e["id"]]["score"] for e in pool if e["event_type"] in config.CHANGE_TYPES
                and e["id"] in staged_ids and _dt(e["occurred_at"]) <= lens.incident_start]
        return RetrievalResult(context, [e["id"] for e, _, _ in chosen], explanations, self.count(context) if context else 0,
                               arch_ids, max(tops) if tops else None)

    # ------------------------------------------------------------------ micro stages
    def ranked_changes(self, lens: Lens, question: str) -> list[dict[str, Any]]:
        """Prod changes of the lens neighbourhood before the incident, best score first."""
        pool = [e for e in self.pool(lens) if e["event_type"] in config.CHANGE_TYPES
                and e.get("environment") == lens.environment and _dt(e["occurred_at"]) <= lens.incident_start]
        sims = self.vectors.similarity(f"{question} {lens.service}", [e["id"] for e in pool])
        qk = keywords(question)
        return sorted(pool, key=lambda e: -self.score(e, sims.get(e["id"], 0.0), lens, qk)["score"])

    def target_changes(self, lens: Lens, ledger_text: str, question: str = "") -> list[dict[str, Any]]:
        """The macro root cause(s) to drill into: CONFIRMED change ids in the ledger, else the lens
        focus, else the best-scoring change."""
        confirmed = []
        for line in ledger_text.splitlines():
            if line.startswith("CONFIRMED"):
                confirmed += [i for i in _IDS.findall(line) if i.startswith("evt_")]
        ids = confirmed or ([lens.focus] if lens.focus and lens.focus.startswith("evt_") else [])
        evs = [e for e in self.store.get_events(ids=ids) if e["event_type"] in config.CHANGE_TYPES]
        return evs or self.ranked_changes(lens, question)[:1]

    def knowledge(self, lens: Lens, question: str, stage: str, n: int = 2,
                  extra: str = "") -> list[dict[str, Any]]:
        """Team knowledge for a stage: release notes of the incident's train (CHANGE_TIMELINE),
        the codebase guide (SUSPECTS / DIFF_ANALYSIS), runbooks and past postmortems (RUNTIME /
        HYPOTHESIS / REMEDIATION), topology (SCOPE / BLAST_RADIUS)."""
        from ff.ingest.cycles import cycle_id

        kinds = KNOWLEDGE_KINDS.get(stage)
        if not kinds:
            return []
        query = f"{question} {lens.service} {extra}"
        if "release" in kinds:
            query += f" {cycle_id(lens.incident_start)} release train"
        from ff.store.sqlite_store import to_epoch

        return self.vectors.query_architecture(query, config.neighbourhood(lens.service), n=n, kinds=kinds,
                                               before=to_epoch(lens.incident_start.isoformat()))

    def incident_evidence(self, lens: Lens) -> tuple[dict[str, Any] | None, str]:
        """The anomaly nearest the incident start in the lens service, and its evidence text
        (templates + exemplar lines of its error signatures)."""
        lo, hi = lens.incident_start - timedelta(hours=6), lens.incident_start + timedelta(hours=3)
        from ff.ingest.scenarios import blast_radius

        # the alerting service may be upstream of the lens after a lens redirect: look at the lens
        # service, its dependencies and everything that depends on it
        services = list(dict.fromkeys(list(config.neighbourhood(lens.service)) + blast_radius(lens.service)))
        anomalies = self.store.get_events(services=services, types=["ANOMALY"], start=lo.isoformat(),
                                          end=hi.isoformat())
        if not anomalies:
            return None, ""
        a = min(anomalies, key=lambda e: abs((_dt(e["occurred_at"]) - lens.incident_start).total_seconds()))
        sigs = self.store.get_events(ids=(a.get("payload") or {}).get("error_signatures", []))
        text = " ".join((a["payload"].get("templates") or []) + [
            " ".join((s.get("payload") or {}).get("exemplars", [])) for s in sigs])
        return a, text

    def suspect_hunks(self, lens: Lens, targets: list[dict[str, Any]], question: str = "") -> list[dict[str, Any]]:
        """Diff hunks of the target changes, plus hunks elsewhere in the window that reference a
        feature flag the targets flipped (a dormant code path), ranked by suspicion."""
        from ff.retrieve import diff_ranker

        cds = self.store.get_change_details([t["id"] for t in targets])
        flags = [c["param_key"].split(".", 1)[1] for c in cds if (c.get("param_key") or "").startswith("feature.")]
        if flags:
            services = {t["service"] for t in targets}
            window = self.store.get_events(services=sorted(services), types=["DEPLOY", "PATCH"],
                                           start=lens.window_start.isoformat(), end=lens.window_end.isoformat(),
                                           environment=lens.environment)
            for c in self.store.get_change_details([e["id"] for e in window]):
                if any(f in (c.get("diff") or "") for f in flags) and c not in cds:
                    cds.append(c)
        _, evidence = self.incident_evidence(lens)
        return diff_ranker.score_hunks(cds, f"{evidence} {question}")

    def micro_retrieve(self, lens: Lens, question: str, ledger_text: str, anchored: set[str], budget: int,
                       stage: str) -> RetrievalResult:
        """DIFF_ANALYSIS: suspect hunks of the root-cause change + the error evidence.
        BLAST_RADIUS: anomalies / error signatures in every service that depends on the lens
        service (and hosted ones) around the incident, + topology. REMEDIATION: the change, its
        top hunk, the version / value to restore, + the runbook."""
        targets = self.target_changes(lens, ledger_text, question)
        hunks: list[dict[str, Any]] = []
        records: list[tuple[str, str]] = []  # (id, text)
        explanations: list[dict[str, Any]] = []
        anomaly, _ = self.incident_evidence(lens)
        if stage in (st.DIFF_ANALYSIS, st.REMEDIATION):
            for t in targets:
                if t["id"] not in anchored:
                    records.append((t["id"], self.compress(t)))
            hunks = self.suspect_hunks(lens, targets, question)
            for h in hunks[: (6 if stage == st.DIFF_ANALYSIS else 2)]:
                head = (f"[{h['change_id']}] {h['file']}:{h['line']} {h['symbol']} (in [{h['event_id']}]) "
                        f"suspicion {h['score']:.2f}: {'; '.join(h['reasons'])}")
                records.append((h["change_id"], self._truncate(head + "\n" + h["diff"], 140)))
                explanations.append({"id": h["change_id"], "event_type": "HUNK", "service": lens.service,
                                     "sim": h["overlap"], "recency": None, "significance": None, "score": h["score"],
                                     "guaranteed": False, "source": "simulated", "tag": "suspect line",
                                     "file": h["file"], "line": h["line"]})
            if anomaly is not None and stage == st.DIFF_ANALYSIS:
                records.append((anomaly["id"], self.compress(anomaly)))
            if stage == st.DIFF_ANALYSIS and self.flags.architecture:
                files = " ".join(h["file"] for h in hunks[:2])
                for ch in self.knowledge(lens, question, stage, n=1, extra=files):
                    records.append((ch["id"], self._truncate(f"[{ch['kind']}] {ch['text']}", 90)))
            if stage == st.REMEDIATION:
                for t in targets:
                    if t["event_type"] in ("DEPLOY", "PATCH") and t.get("version_from"):
                        records.append((t["id"], f"[{t['id']}] rollback target: {t['service']} {t['version_to']} -> "
                                                 f"{t['version_from']}"))
                for ln in ledger_text.splitlines():
                    if ln.startswith("CONTRIBUTING"):
                        for i in _IDS.findall(ln):
                            ev = self.store.get_event(i)
                            if ev:
                                records.append((i, self.compress(ev)))
        else:  # BLAST_RADIUS
            from ff.ingest.scenarios import blast_radius

            services = list(dict.fromkeys(blast_radius(lens.service) + list(config.neighbourhood(lens.service))))
            lo, hi = lens.incident_start - timedelta(hours=2), lens.incident_start + timedelta(hours=3)
            evs = self.store.get_events(services=services, types=list(config.LOG_TYPES), start=lo.isoformat(),
                                        end=hi.isoformat())
            evs.sort(key=lambda e: (e["event_type"] != "ANOMALY", e["occurred_at"]))
            for e in evs[:10]:
                records.append((e["id"], self.compress(e)))
            if self.flags.architecture:
                for ch in self.knowledge(lens, question, stage, n=1):
                    records.append((ch["id"], self._truncate(f"[{ch['kind']}] {ch['text']}", 90)))
            hosts = [f"{s} runs on {h}" for s, h in config.TOPOLOGY_HOSTS.items() if s in services or h in services]
            feeds = [f"{p} publishes to {q}" for q, ps in config.QUEUE_PRODUCERS.items() for p in ps
                     if q in services or p in services]
            records.append(("topology", "Topology: " + "; ".join(
                [f"{s} calls {', '.join(config.dependencies(s))}" for s in services if config.dependencies(s)]
                + hosts + feeds)))
        if stage == st.REMEDIATION and self.flags.architecture:
            for ch in self.knowledge(lens, question, stage, n=2):
                records.append((ch["id"], self._truncate(f"[{ch['kind']}] {ch['text']}", 100)))
        # small models echo what is closest to the question: end the diff context with the top suspect
        summary = None
        if stage == st.DIFF_ANALYSIS and hunks:
            h = hunks[0]
            summary = (h["change_id"], f"Top suspect line: [{h['change_id']}] {h['file']}:{h['line']} ({h['symbol']}), "
                                       f"suspicion {h['score']:.2f}: {h['reasons'][0]}.")
        reserve = self.count(summary[1]) + 1 if summary else 0
        chosen, used = [], 0
        for rid, text in records:
            if rid in anchored and rid.startswith("evt_"):
                continue
            n = self.count(text) + 1
            if used + n <= budget - reserve:
                chosen.append((rid, text))
                used += n
        if summary and used + reserve <= budget:
            chosen.append(summary)
        context = "\n".join(t for _, t in chosen)
        ids = [r for r, _ in chosen if r.startswith(("evt_", "chg_"))]
        return RetrievalResult(context, list(dict.fromkeys(ids)), explanations, self.count(context) if context else 0,
                               [r for r, _ in chosen if r.startswith("doc:")], None)

    def plain_retrieve(self, question: str, anchored_ids: Iterable[str] = (), budget: int = config.RETRIEVAL_BUDGET,
                       k: int = 8) -> RetrievalResult:
        """Standard RAG: top-k events for the question, no lens / stage / guarantees."""
        anchored = set(anchored_ids)
        hits = [(i, s) for i, s in self.vectors.query_events(question, n=k + len(anchored)) if i not in anchored][:k]
        evs = {e["id"]: e for e in self.store.get_events(ids=[i for i, _ in hits])}
        cds_by_event: dict[str, list[dict[str, Any]]] = {}
        for c in self.store.get_change_details(list(evs)):
            cds_by_event.setdefault(c["event_id"], []).append(c)
        chosen, used = [], 0
        for i, s in hits:
            e = evs.get(i)
            if e is None:
                continue
            rec = self.render_record(e, cds_by_event.get(i, []))
            n = self.count(rec) + 1
            if used + n <= budget:
                chosen.append((e, rec, s))
                used += n
        chosen.sort(key=lambda c: (c[0]["occurred_at"], c[0]["id"]))
        context = "\n".join(rec for _, rec, _ in chosen)
        expl = [{"id": e["id"], "event_type": e["event_type"], "service": e["service"], "sim": round(s, 3),
                 "recency": None, "significance": e["significance"], "score": round(s, 3), "guaranteed": False,
                 "source": e["source"], "tag": "plain"} for e, _, s in chosen]
        return RetrievalResult(context, [e["id"] for e, _, _ in chosen], expl, self.count(context) if context else 0)

    # ------------------------------------------------------------------ session start
    def checkpoint_candidates(self, lens: Lens, initial_query: str, k: int = config.CHECKPOINT_CANDIDATES) -> list[dict[str, Any]]:
        """Up to ``k`` anchor candidates with a one-line reason each (guaranteed changes pre-ticked)."""
        pool = self.pool(lens)
        cands: dict[str, dict[str, Any]] = {}

        def add(e: dict[str, Any], reason: str, preselect: bool = False) -> None:
            if e["id"] not in cands and len(cands) < k:
                cands[e["id"]] = {"id": e["id"], "event_type": e["event_type"], "service": e["service"],
                                  "source": e["source"], "reason": reason, "preselect": preselect,
                                  "compressed": self.compress(e), "occurred_at": e["occurred_at"],
                                  "significance": e["significance"]}

        for i, e in enumerate(self.guaranteed(lens, pool)):
            add(e, f"{self._env_label(e)} {e['event_type'].lower().replace('_', ' ')} "
                   f"{self._rel_time(e, lens)} (major change)", preselect=i < 3)
        anomalies = sorted((e for e in pool if e["event_type"] == "ANOMALY"),
                           key=lambda e: abs((_dt(e["occurred_at"]) - lens.incident_start).total_seconds()))
        for e in anomalies[:3]:
            add(e, f"error burst from logs in {e['service']}, {self._rel_time(e, lens)}")
        sims = self.vectors.similarity(initial_query, [e["id"] for e in pool])
        for i in sorted(sims, key=lambda x: -sims[x])[:3]:
            e = next(p for p in pool if p["id"] == i)
            add(e, f"similar to the initial question (sim {sims[i]:.2f})")
        return list(cands.values())

    @staticmethod
    def _env_label(e: dict[str, Any]) -> str:
        return e.get("environment") or "prod"

    @staticmethod
    def _rel_time(e: dict[str, Any], lens: Lens) -> str:
        delta = lens.incident_start - _dt(e["occurred_at"])
        sign = "before" if delta >= timedelta(0) else "after"
        s = abs(delta.total_seconds())
        if s < 3600:
            amount = f"{int(s // 60)} min"
        elif s < 86400 * 2:
            amount = f"{s / 3600:.1f} h"
        else:
            amount = f"{s / 86400:.0f} days"
        return f"{amount} {sign}"
