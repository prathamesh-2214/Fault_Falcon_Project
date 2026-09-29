"""Fixed-template narratives for simulated changes, and rule-based significance (1-5).

Every field is filled deterministically. TinyLlama writes ``change_summary`` only when the
commit messages are longer than 30 words; otherwise they are joined verbatim.

Template::

    [{event_type}] {service} {version_from} -> {version_to} in {environment} at {timestamp}
    What changed: {change_summary}
    Why: {reason or 'not stated'}
    Parameters changed: {param}: {old} -> {new} ({reason}); ...
    Tests: {passed}/{total} passed; failed: {failed}; skipped: {skipped}
    Security: {counts by severity}; new since last deploy: {new}
    Post-deploy (30 min, from logs): {line from the simulator}
    Structural details: event_id={event_id}
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable
from datetime import datetime
from typing import Any

from ff import config

LLMFn = Callable[[str, str], str]
SUMMARY_SYSTEM = "Summarise these commit messages in one sentence. Use only the facts given."
RISKY_PARAM_PREFIXES = ("db.", "http.timeout", "retry", "jvm.heap", "gc.", "feature.", "cache.", "consumer.",
                        "webhook.", "breaker.", "session.")


def is_risky_param(key: str | None) -> bool:
    """pool / timeout / retry / heap / gc / feature-flag parameters."""
    return bool(key) and key.startswith(RISKY_PARAM_PREFIXES)


def significance(ev: dict[str, Any]) -> int:
    """Significance 1-5 by rules, never by the LLM.

    5 = prod INFRA_CHANGE, ROLLBACK, schema change
    4 = prod DEPLOY/PATCH; CONFIG_CHANGE to pool/timeout/retry/heap/flag
    3 = PPD/test deploy with failed or skipped suites; new high/critical vuln;
        ANOMALY with error ratio > 3x baseline (and its error signatures)
    2 = routine green PPD/test deploy; minor ANOMALY; other CONFIG_CHANGE
    1 = informational
    """
    t, env, p = ev["event_type"], ev.get("environment", "prod"), ev.get("payload") or {}
    prod = env == "prod"
    if t in ("INFRA_CHANGE", "ROLLBACK"):
        return 5 if prod else 3
    if t in ("DEPLOY", "PATCH"):
        if p.get("schema_change") and prod:
            return 5
        if prod:
            return 4
        tests = p.get("tests") or {}
        return 3 if (tests.get("failed") or tests.get("skipped")) else 2
    if t == "CONFIG_CHANGE":
        return 4 if any(is_risky_param(x.get("key")) for x in p.get("params", [])) else 2
    if t == "SECURITY_SCAN":
        return 3 if p.get("new_high_critical") else 1
    if t == "TEST_RUN":
        return 3 if (p.get("failed") or p.get("skipped")) else 1
    if t == "ANOMALY":
        r = p.get("ratio_vs_baseline")
        return 3 if (r is None and p.get("error_lines", 0) > 0) or (r is not None and r > config.ERROR_RATIO_MULT) else 2
    if t == "ERROR_SIGNATURE":
        r = p.get("anomaly_ratio_vs_baseline")
        return 3 if r is None or r > config.ERROR_RATIO_MULT else 2
    return 1


def _ts(iso: str) -> str:
    return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M UTC")


def change_summary(messages: list[str], llm: LLMFn | None = None, cache: Any | None = None) -> str:
    """Join short commit messages; ask the LLM for one sentence only if they exceed 30 words."""
    joined = "; ".join(m.strip() for m in messages if m.strip()) or "not stated"
    if llm is None or len(joined.split()) <= 30:
        return joined
    key = "sum:" + hashlib.sha256(joined.encode()).hexdigest()
    if cache is not None and (hit := cache.cache_get(key)):
        return hit
    out = " ".join(llm(SUMMARY_SYSTEM, joined).split()[:40]).strip() or joined
    if cache is not None:
        cache.cache_put(key, out)
    return out


def _params_line(params: list[dict[str, Any]]) -> str:
    if not params:
        return "none"
    return "; ".join(f"{p['key']}: {p['old']} -> {p['new']} ({p.get('reason') or 'not stated'})" for p in params)


def _tests_line(t: dict[str, Any] | None) -> str:
    if not t:
        return "none linked"
    line = f"{t['passed']}/{t['total']} passed; failed: {t['failed']}; skipped: {t['skipped']}"
    if t.get("skipped_suites"):
        line += f" (skipped suites: {', '.join(t['skipped_suites'])})"
    if t.get("failed_suites"):
        line += f" (failed suites: {', '.join(t['failed_suites'])})"
    return line


def _security_line(s: dict[str, Any] | None) -> str:
    if not s:
        return "no scan linked"
    counts = ", ".join(f"{k} {s['counts'].get(k, 0)}" for k in ("critical", "high", "medium", "low"))
    return f"{counts}; new since last deploy: {s.get('new_since_last_deploy', 0)}"


def render_change(ev: dict[str, Any], llm: LLMFn | None = None, cache: Any | None = None) -> str:
    """Narrative for DEPLOY / PATCH / ROLLBACK / CONFIG_CHANGE / INFRA_CHANGE."""
    p = ev.get("payload") or {}
    summary = change_summary(p.get("commit_messages", []), llm, cache) if p.get("commit_messages") else (
        p.get("change_summary") or "not stated")
    vf, vt = ev.get("version_from") or "n/a", ev.get("version_to") or "n/a"
    lines = [
        f"[{ev['event_type']}] {ev['service']} {vf} -> {vt} in {ev.get('environment', 'prod')} at {_ts(ev['occurred_at'])}",
        f"What changed: {summary}",
        f"Why: {p.get('reason') or 'not stated'}",
        f"Parameters changed: {_params_line(p.get('params', []))}",
        f"Tests: {_tests_line(p.get('tests'))}",
        f"Security: {_security_line(p.get('security'))}",
        f"Post-deploy (30 min, from logs): {p.get('post_deploy') or 'no log coverage'}",
    ]
    cr = p.get("change_request") or {}
    if cr:
        lines.append(f"Change request: {cr.get('id')} by {cr.get('author')} (approved by {cr.get('approver')}, risk "
                     f"{cr.get('risk')}); rollout: {cr.get('rollout')}; rollback plan: {cr.get('rollback_plan')}")
    if p.get("bug"):
        lines.append(f"Bug fixed: {p['bug']}")
    if p.get("description"):
        lines.append("Description: " + " ".join(p["description"].split())[:700])
    lines.append(f"Structural details: event_id={ev['id']}")
    return "\n".join(lines)


def render_test_run(ev: dict[str, Any]) -> str:
    p = ev["payload"]
    suites = "; ".join(
        f"{name} skipped" if s.get("skipped_suite") else f"{name} {s['passed']}/{s['passed'] + s['failed']}"
        + (f" ({s['failed']} failed)" if s["failed"] else "")
        for name, s in p["suites"].items())
    return (f"[TEST_RUN] {ev['service']} {ev.get('version_to') or ''} in {ev.get('environment')} at {_ts(ev['occurred_at'])}\n"
            f"Suites: {suites}; coverage delta {p['coverage_delta']:+.1f}%\n"
            f"Totals: {p['passed']}/{p['total']} passed; failed: {p['failed']}; skipped: {p['skipped']}\n"
            f"Structural details: event_id={ev['id']}")


def render_security_scan(ev: dict[str, Any]) -> str:
    p = ev["payload"]
    return (f"[SECURITY_SCAN] {ev['service']} at {_ts(ev['occurred_at'])}\n"
            f"Findings: {_security_line(p)}\n"
            f"Structural details: event_id={ev['id']}")


def render(ev: dict[str, Any], llm: LLMFn | None = None, cache: Any | None = None) -> str:
    """Narrative for any simulated event type."""
    t = ev["event_type"]
    if t == "TEST_RUN":
        return render_test_run(ev)
    if t == "SECURITY_SCAN":
        return render_security_scan(ev)
    if t in config.CHANGE_TYPES:
        return render_change(ev, llm, cache)
    raise ValueError(f"not a simulated event type: {t}")


def summarize_all(events: Iterable[dict[str, Any]], change_details: Iterable[dict[str, Any]],
                  llm: LLMFn | None = None, cache: Any | None = None) -> list[dict[str, Any]]:
    """Attach ``params`` (from change details), ``narrative`` and ``significance`` to simulated events."""
    by_event: dict[str, list[dict[str, Any]]] = {}
    for cd in change_details:
        by_event.setdefault(cd["event_id"], []).append(cd)
    out = []
    for ev in events:
        ev = json.loads(json.dumps(ev))  # deep copy, JSON-safe
        if ev["source"] != "simulated":
            out.append(ev)
            continue
        cds = by_event.get(ev["id"], [])
        ev.setdefault("payload", {})["params"] = [
            {"key": c["param_key"], "old": c["param_old"], "new": c["param_new"], "reason": c.get("param_reason")}
            for c in cds if c.get("param_key")]
        ev["payload"]["files"] = sorted({c["file"] for c in cds if c.get("file")})
        ev["narrative"] = render(ev, llm, cache)
        ev["significance"] = significance(ev)
        out.append(ev)
    return out
