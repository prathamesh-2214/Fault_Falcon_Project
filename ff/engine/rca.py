"""The RCA report: a structured postmortem built from what the investigation established.

It uses ONLY the session's evidence, never the simulator's hidden ground truth:

* the ledger (CONFIRMED change = macro root cause, CONFIRMED ``chg_`` hunk = breaking line,
  CONTRIBUTING, RULED_OUT),
* the stores (the anomaly and error signatures from the real logs, the changes, their diffs),
* the topology (blast radius) and the model's answers per stage (quoted as analyst notes).

Remediation and action items are derived from the confirmed change type (rollback a deploy,
revert a config key, restore a terraform attribute, fix the line) and from its evidence
(e.g. a skipped test suite).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any


@dataclass
class RCAReport:
    """Sections of the report plus the rendered markdown."""

    title: str
    sections: dict[str, str] = field(default_factory=dict)
    root_cause_id: str | None = None
    breaking_change_id: str | None = None
    complete: bool = False

    @property
    def markdown(self) -> str:
        return f"# {self.title}\n\n" + "\n\n".join(f"## {k}\n\n{v}" for k, v in self.sections.items()) + "\n"


def _fmt_t(iso: str | None) -> str:
    return datetime.fromisoformat(iso).strftime("%Y-%m-%d %H:%M UTC") if iso else "?"


def affected_services(service: str) -> list[str]:
    from ff.ingest.scenarios import blast_radius

    return blast_radius(service)


def remediation_for(ev: dict[str, Any], cds: list[dict[str, Any]], breaking: dict[str, Any] | None) -> list[str]:
    """Concrete fix steps for a confirmed root-cause change."""
    steps = []
    t, svc = ev["event_type"], ev["service"]
    if t in ("DEPLOY", "PATCH"):
        steps.append(f"Roll back {svc} from {ev.get('version_to')} to {ev.get('version_from')} (rollback target "
                     f"[{ev['id']}]).")
    elif t == "ROLLBACK":
        steps.append(f"Complete the rollback of {svc} to {ev.get('version_to')} on every node.")
    for c in cds:
        if c.get("param_key"):
            where = f"{c['file']}:{c['line_start']}"
            if t == "INFRA_CHANGE":
                steps.append(f"Terraform: restore `{c['param_key']} = {c['param_old']}` on {c.get('infra_resource')} "
                             f"({where}), then `terraform apply`.")
            else:
                steps.append(f"Revert `{c['param_key']}` from {c['param_new']} to {c['param_old']} in {where}.")
    if breaking is not None and not breaking.get("param_key"):
        removed = [ln[1:].strip() for ln in (breaking.get("diff") or "").splitlines()
                   if ln.startswith("-") and not ln.startswith("---")]
        fix = f"restore `{removed[0]}`" if removed else "remove the added lines"
        steps.append(f"Code fix at {breaking['file']}:{breaking['line_start']} ({breaking.get('symbol')}): {fix}.")
    return steps


def action_items(ev: dict[str, Any] | None, breaking: dict[str, Any] | None, template: str | None) -> list[str]:
    items = []
    if ev is not None:
        p = ev.get("payload") or {}
        for env, tests in (p.get("release_tests") or {}).items():
            for suite in tests.get("skipped_suites", []):
                items.append(f"Make the {env} {suite} test suite mandatory before prod.")
        if ev["event_type"] == "CONFIG_CHANGE":
            items.append("Add validation bounds and a canary for risky config keys (pool / timeout / retry / flags).")
        if ev["event_type"] == "INFRA_CHANGE":
            items.append("Require a reviewed `terraform plan` and a CloudWatch alarm for the changed AWS resource.")
    if breaking is not None and not breaking.get("param_key"):
        items.append(f"Add a regression test covering {breaking.get('symbol')} ({breaking['file']}).")
    if template:
        items.append(f"Alert on the error signature `{template[:80]}`.")
    return items or ["Review the detector thresholds for this service."]


def build_rca(session: Any, store: Any) -> RCAReport:
    """Build the report from a :class:`~ff.engine.session.Session` and the SQLite store."""
    lens = session.lens
    led = session.ledger
    confirmed = led.ids_of("CONFIRMED")
    macro_id = next((i for i in confirmed if i.startswith("evt_")), None)
    micro_id = next((i for i in confirmed if i.startswith("chg_")), None)
    macro = store.get_event(macro_id) if macro_id else None
    macro_cds = store.get_change_details([macro_id]) if macro_id else []
    breaking = store.get_change_detail(micro_id) if micro_id else None
    lo, hi = lens.incident_start - timedelta(hours=6), lens.incident_start + timedelta(hours=3)
    anomalies = store.get_events(services=[lens.service], types=["ANOMALY"], start=lo.isoformat(), end=hi.isoformat())
    anomaly = min(anomalies, key=lambda e: abs((datetime.fromisoformat(e["occurred_at"]) - lens.incident_start)
                                               .total_seconds())) if anomalies else None
    ap = (anomaly or {}).get("payload") or {}
    template = (ap.get("templates") or [None])[0]
    rep = RCAReport(f"RCA: {lens.service} incident at {lens.incident_start:%Y-%m-%d %H:%M} UTC",
                    root_cause_id=macro_id, breaking_change_id=micro_id)
    s = rep.sections
    status = "Root cause confirmed" if macro_id else "Root cause NOT yet confirmed (use Confirm on a change)"
    s["Summary"] = (f"**Status:** {status}. **Engineer's question:** {session.initial_query}\n\n"
                    + (f"**Root cause (macro):** [{macro_id}] {macro['event_type']} {macro['service']} "
                       f"{macro.get('version_from') or ''} -> {macro.get('version_to') or ''} at "
                       f"{_fmt_t(macro['occurred_at'])}.\n\n" if macro else "")
                    + (f"**Breaking line (micro):** [{micro_id}] `{breaking['file']}:{breaking['line_start']}` "
                       f"({breaking.get('symbol')}).\n\n" if breaking else "**Breaking line (micro):** not confirmed yet.\n\n"))
    impacted = affected_services(lens.service)
    impact = [f"- Affected service: **{lens.service}**; upstream / hosted services at risk: "
              f"{', '.join(x for x in impacted if x != lens.service) or 'none'}."]
    if anomaly:
        impact.append(f"- Real-log anomaly [{anomaly['id']}] {_fmt_t(anomaly['occurred_at'])} -> "
                      f"{_fmt_t(anomaly.get('end_at'))}: {ap.get('error_lines')} of {ap.get('lines')} lines non-INFO, "
                      f"peak error ratio {ap.get('peak_error_ratio', 0):.2f} vs baseline median "
                      f"{ap.get('baseline_median_error_ratio', 0):.2f}.")
        if template:
            impact.append(f"- Top error signature: `{template[:120]}`")
        impact.append(f"- Detected by: {', '.join(ap.get('reasons', [])) or 'n/a'} (window significance rules).")
    s["Impact and detection"] = "\n".join(impact)
    timeline = []
    for eid in dict.fromkeys(led.cited_ids()):
        if eid.startswith("evt_"):
            ev = store.get_event(eid)
            if ev:
                kinds = [ln.kind for ln in led.lines if eid in ln.ids]
                timeline.append((ev["occurred_at"], f"- {_fmt_t(ev['occurred_at'])} [{eid}] {ev['event_type']} "
                                                    f"{ev['service']} {ev.get('version_from') or ''} -> "
                                                    f"{ev.get('version_to') or ''} ({', '.join(kinds).lower()})"))
    if anomaly:
        timeline.append((anomaly["occurred_at"], f"- {_fmt_t(anomaly['occurred_at'])} [{anomaly['id']}] anomaly starts "
                                                 f"in {lens.service}"))
    s["Timeline"] = "\n".join(t for _, t in sorted(timeline)) or "- (no events cited yet)"
    if macro:
        rc = [f"[{macro_id}] {macro['event_type']} in {macro['service']} at {_fmt_t(macro['occurred_at'])}."]
        rc += [f"- `{c['file']}`: {c['param_key']} {c['param_old']} -> {c['param_new']}" if c.get("param_key")
               else f"- `{c['file']}:{c['line_start']}` ({c.get('symbol')})" for c in macro_cds]
        pd_line = (macro.get("payload") or {}).get("post_deploy")
        if pd_line and not pd_line.startswith("no log"):
            rc.append(f"- Post-deploy error rate: {pd_line}")
        s["Root cause (macro)"] = "\n".join(rc)
    else:
        s["Root cause (macro)"] = "Not confirmed. In the chat, use **Confirm** on the change you believe caused it."
    if breaking:
        s["Breaking line (micro)"] = (f"[{micro_id}] `{breaking['file']}:{breaking['line_start']}` "
                                      f"({breaking.get('symbol')}), part of [{breaking['event_id']}]:\n\n"
                                      f"```diff\n{breaking.get('diff') or ''}\n```")
    else:
        s["Breaking line (micro)"] = "Not confirmed. Use the Diff & suspect lines tab to confirm the hunk."
    contrib = [f"- [{i}] {store.get_event(i)['event_type']} {store.get_event(i)['service']}"
               for i in led.ids_of("CONTRIBUTING") if i.startswith("evt_") and store.get_event(i)]
    ruled = [f"- [{i}] {ln.text}" for ln in led.lines if ln.kind == "RULED_OUT" for i in ln.ids]
    s["Contributing factors"] = "\n".join(contrib) or "- none recorded"
    s["Ruled out"] = "\n".join(ruled) or "- none recorded"
    fix = remediation_for(macro, macro_cds, breaking) if macro else []
    s["Remediation"] = "\n".join(f"{n}. {x}" for n, x in enumerate(fix, 1)) or "Pending a confirmed root cause."
    s["Action items"] = "\n".join(f"- [ ] {x}" for x in action_items(macro, breaking, template))
    notes = []
    for q, a in zip(session.history[0::2], session.history[1::2]):
        notes.append(f"- **Q:** {q[1][:160]}\n  **A:** {a[1][:400]}")
    s["Analyst notes (model answers)"] = "\n".join(notes[-6:]) or "- none"
    rep.complete = bool(macro and breaking)
    return rep

