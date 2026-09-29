"""Micro context: rank the diff hunks of a suspect change by how likely each broke production.

Once the macro root cause (a change) is known, the question becomes *which line*. Each hunk
(``change_detail`` row with a unified diff) is scored against the incident's evidence (the
real error templates / exemplars and the engineer's question):

    score = 0.35 magnitude + 0.30 concept overlap + 0.20 risky tokens + 0.15 file relevance

* magnitude: relative size of a numeric parameter change, a port / type / flag flip, a changed
  comparison or constant, a removed call (e.g. ``free``); ~0 for logging / comments / docs;
* concept overlap: both sides mapped onto operational concepts (timeout, connection, retry,
  port, disk, memory, latency, pool, capacity) so ``to_port = 2888`` meets
  "Cannot open channel ... election address";
* risky tokens: timeout, pool, backoff, port, throughput, heap, free, flags, sync, batch ...;
* relevance: words shared by the file path / symbol and the evidence.

Deterministic, explainable (every score comes with reasons) and testable against the
simulator's ground truth (``ff/eval/micro_check.py``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

CONCEPTS: dict[str, tuple[str, ...]] = {
    "timeout": ("timeout", "time out", "timed out", "timeout_ms", "deadline", "/ 1000"),
    "connection": ("connection", "connect", "channel", "refused", "broken", "no route", "socket", "cnxn"),
    "retry": ("retry", "retries", "backoff", "attempt", "renew", "contacting", "lease"),
    "port": ("port", "election", "ingress", "security_group", "to_port", "2888", "3888", "2181"),
    "disk": ("disk", "storage", "write", "blk", "block", "throughput", "iops", "ebs", "volume", "serving", "flush"),
    "db": ("alter table", "migration", "lock", "database", "jobs", "sql", "rds", "query", "db."),
    "memory": ("memory", "heap", "tlb", "free(", "leak", "buffer", "image_buf", "instance_type", "oom"),
    "latency": ("latency", "health_check", "sync", "verify", "base file", "image", "slow"),
    "pool": ("pool", "db", "rds", "instance_class", "max_size"),
    "capacity": ("slots", "capacity", "scheduler", "task", "container", "reserve", "freeslots", "ack"),
    "load": ("error loading", "program image", "ciod", "load"),
    "null": ("null", "none", "nil", "nullpointer", "nonetype", "nil pointer", "invalid memory address"),
    "lock": ("lock", "synchronized", "mutex", "contention"),
    "schema": ("field", "schema", "unrecognized", "parse", "json", "flavor", "instance_type", "event"),
    "cache": ("cache", "evict", "ttl", "session", "redis", "put(", "append(", "local["),
    "queue": ("sqs", "queue", "visibility", "redrive", "dlq", "receive"),
    "auth": ("password", "credential", "secret", "rotation", "accessdenied", "403", "iam", "token", "401"),
    "throttle": ("throttl", "burst", "rate", "429", "concurrency", "too many"),
}
RISKY = ("timeout", "pool", "retry", "backoff", "port", "throughput", "iops", "heap", "free(", "buffer", "isOn(",
         "flags", "sync", "batch", "page_size", "size", "cached", "instance", "attempt", "rc <", "alter table",
         "not null", "flush", "expire", "lock", "synchronized", "mutex", "null", "nil", "none", "range", "ttl",
         "<version>", "==", "v9.", "v1.")
BENIGN_LINE = re.compile(r"^\s*(//|#(?!define|include|if)|/\*|\*|LOG\.|log\.|printk|pr_debug|metrics\.|self\._metrics|- |\"refresh\"|See )")
_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_WORD = re.compile(r"[a-z][a-z0-9_]{3,}")


def concepts(text: str) -> set[str]:
    low = text.lower()
    return {c for c, words in CONCEPTS.items() if any(w in low for w in words)}


def changed_lines(diff: str) -> tuple[list[str], list[str]]:
    """(removed, added) code lines of a unified diff (without the +/- marker)."""
    rem, add = [], []
    for ln in (diff or "").splitlines():
        if ln.startswith("@@"):
            continue
        if ln.startswith("-"):
            rem.append(ln[1:])
        elif ln.startswith("+"):
            add.append(ln[1:])
    return rem, add


def _num(s: Any) -> float | None:
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def magnitude(cd: dict[str, Any]) -> tuple[float, str]:
    """How big / behaviour-changing the hunk is, with a reason."""
    if cd.get("param_key"):
        old, new = _num(cd.get("param_old")), _num(cd.get("param_new"))
        if old is not None and new is not None:
            r = abs(new - old) / max(abs(old), 1.0)
            if "port" in cd["param_key"]:
                return 1.0, f"port {cd['param_old']} -> {cd['param_new']}"
            return min(1.0, r), f"{cd['param_key']} {cd['param_old']} -> {cd['param_new']} ({(new - old) / max(abs(old), 1):+.0%})"
        return 0.6, f"{cd['param_key']} {cd.get('param_old')} -> {cd.get('param_new')}"
    rem, add = changed_lines(cd.get("diff") or "")
    lines = rem + add
    if not lines or all(BENIGN_LINE.match(x) or not x.strip() for x in lines):
        return 0.05, "logging / comments only"
    if (cd.get("file") or "").endswith((".md", ".json")):
        return 0.05, "docs / dashboards"
    removed_calls = [x.strip() for x in rem if re.search(r"\w+\(", x) and not any(x.strip() in a for a in add)]
    nums_changed = {m for x in rem for m in _NUM.findall(x)} ^ {m for x in add for m in _NUM.findall(x)}
    ops_changed = any(o in " ".join(rem) for o in ("<", ">", "*", "not ")) and " ".join(rem) != " ".join(add)
    if (cd.get("file") or "").endswith(".sql"):
        body = " ".join(add).upper()
        if any(k in body for k in ("ALTER TABLE", "DROP ", "NOT NULL", "LOCK")) or (
                "CREATE INDEX" in body and "CONCURRENTLY" not in body and "IF NOT EXISTS" not in body):
            return 0.9, "blocking schema migration"
        return 0.2, "schema migration (index / additive)"
    name = (cd.get("file") or "").rsplit("/", 1)[-1]
    if name in ("pom.xml", "go.mod", "requirements.txt", "Makefile.deps"):
        return 0.85, "upgrades a library version"
    guard = re.compile(r"(if|==|is None|== nil|== null|!).*(null|None|nil)|if \(!|is None|== nil|== null")
    if rem and not add and any(guard.search(x) for x in rem):
        return 0.85, "removes a null / nil guard"
    if any(re.search(r"synchronized|_LOCK|mu\.Lock|Mu\.Lock|mutex_lock", x) for x in add) and not any(
            re.search(r"synchronized|_LOCK|Mu\.Lock|mutex_lock", x) for x in rem):
        return 0.8, "adds a global lock"
    if any(re.search(r"flags\.(isOn|On)\(", x) for x in add):
        return 0.8, "adds a flag-guarded code path"
    keys_rem = {m for x in rem for m in re.findall(r'"(\w+)":', x)}
    keys_add = {m for x in add for m in re.findall(r'"(\w+)":', x)}
    if keys_rem and keys_rem != keys_add:
        return 0.8, f"changes event field `{sorted(keys_rem - keys_add or keys_rem)[0]}`"
    if add and not rem and any(re.search(r"\.put\(|\.append\(|local\[", x) for x in add):
        return 0.7, "adds entries to an unbounded in-memory structure"
    if rem and all(BENIGN_LINE.match(x) or not x.strip() for x in add) and any(re.search(r"\w+\(", x) for x in rem):
        return 0.8, f"removes `{rem[0].strip()[:50]}`"
    if any("free(" in x for x in rem) and not any("free(" in x for x in add):
        return 1.0, "removes a free() call (leak)"
    if any("isOn(" in x for x in add):
        return 0.8, "adds a flag-guarded code path"
    if removed_calls and add:
        return 0.8, f"replaces `{removed_calls[0][:50]}`"
    if nums_changed or ops_changed:
        return 0.75, "changes a constant / comparison"
    if add and not rem:
        return 0.3, "adds lines"
    return 0.35, "edits logic"


def score_hunks(cds: Iterable[dict[str, Any]], evidence: str) -> list[dict[str, Any]]:
    """Rank hunks (change_detail rows with ``diff``) against evidence text; highest first."""
    ev_concepts = concepts(evidence)
    ev_words = set(_WORD.findall(evidence.lower()))
    out = []
    for cd in cds:
        diff = cd.get("diff") or ""
        rem, add = changed_lines(diff)
        changed = "\n".join(rem + add)
        mag, why = magnitude(cd)
        hunk_concepts = concepts(changed + " " + (cd.get("param_key") or "") + " " + (cd.get("infra_resource") or ""))
        overlap = len(hunk_concepts & ev_concepts) / max(1, min(len(ev_concepts), 3)) if ev_concepts else 0.0
        overlap = min(1.0, overlap)
        risky = 1.0 if any(r.lower() in changed.lower() for r in RISKY) else 0.0
        path_words = set(_WORD.findall(((cd.get("file") or "") + " " + (cd.get("symbol") or "")).lower().replace("/", " ")))
        relevance = min(1.0, len(path_words & ev_words) / 2)
        if mag <= 0.05:  # a logging-only edit cannot be "relevant"
            overlap, risky, relevance = overlap * 0.2, 0.0, relevance * 0.2
        score = 0.35 * mag + 0.30 * overlap + 0.20 * risky + 0.15 * relevance
        reasons = [why]
        shared = sorted(hunk_concepts & ev_concepts)
        if shared:
            reasons.append("matches the errors on " + ", ".join(shared))
        if risky:
            reasons.append("touches a risky setting")
        out.append({"change_id": cd["id"], "event_id": cd["event_id"], "file": cd.get("file"),
                    "line": cd.get("line_start"), "symbol": cd.get("symbol"), "score": round(score, 3),
                    "magnitude": round(mag, 2), "overlap": round(overlap, 2), "risky": risky,
                    "relevance": round(relevance, 2), "reasons": reasons, "diff": diff,
                    "param_key": cd.get("param_key")})
    return sorted(out, key=lambda r: (-r["score"], r["change_id"]))
