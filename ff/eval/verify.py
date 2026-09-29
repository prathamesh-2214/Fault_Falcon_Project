"""``make verify``: check the data, the ground truth and the core invariants yourself.

Runs against the ingested data in ``FF_HOME`` (default: the repo) without downloading a model,
prints a PASS / FAIL table and writes ``results/verify_report.md``. Exit code 1 if anything fails.

Checks: real logs untouched, incident count and 80/20 split, every incident detected in the logs,
topology and dependency paths, event sources, id numbering, incident ground truth (cause before the
incident, decoys closer, contributing earlier, a breaking line with a real diff for every caused
incident, mitigation after the start), no no-op causes, parameter chains, prod versions only go
up, post-deploy lines match the log lines, change descriptions, stores consistent, one baseline
per component, the suspect-line ranking finds the breaking line, RCA templates filled, the
knowledge base (release plans, change logs, codebase guides, postmortems published after their
incident), release-train deploy order, cross-service incidents, and StreamingLLM's pos-shift
equivalence + anchor preservation on the tiny model. ``--smoke`` also runs the real-model smoke check.

    python -m ff.eval.verify [--smoke]
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from datetime import datetime
from typing import Any

from ff import config
from ff.config import Paths, get_paths


def _t(x: str) -> datetime:
    return datetime.fromisoformat(x)


class Checks:
    def __init__(self) -> None:
        self.rows: list[tuple[str, bool, str]] = []

    def run(self, name: str, fn: Callable[[], str]) -> None:
        try:
            detail = fn()
            self.rows.append((name, True, detail))
        except AssertionError as e:
            self.rows.append((name, False, str(e) or "assertion failed"))
        except Exception as e:  # noqa: BLE001 - report, do not crash
            self.rows.append((name, False, f"{type(e).__name__}: {e}"))

    @property
    def ok(self) -> bool:
        return all(r[1] for r in self.rows)

    def table(self) -> str:
        lines = ["| check | result | detail |", "| --- | --- | --- |"]
        lines += [f"| {n} | {'PASS' if ok else '**FAIL**'} | {d.replace('|', '/')} |" for n, ok, d in self.rows]
        return "\n".join(lines)


def run(paths: Paths | None = None, smoke: bool = False) -> Checks:
    import pandas as pd

    from ff.ingest.pipeline import open_stores
    from ff.ingest.simulator import load_world

    paths = paths or get_paths()
    c = Checks()
    world = load_world(paths)
    ev = world.by_id()
    store, vectors = open_stores(paths)

    def logs_untouched() -> str:
        counts = []
        for svc, system in config.SERVICE_MAP.items():
            lines = pd.read_parquet(paths.derived / f"lines_{svc}.parquet", columns=["content", "source"])
            real = lines[lines["source"] == "loghub"]
            raw = paths.raw_loghub / system / f"{system}_2k.log_structured.csv"
            if raw.exists():
                raw_df = pd.read_csv(raw, dtype=str, keep_default_na=False)
                assert len(real) == len(raw_df), f"{svc}: {len(real)} derived lines vs {len(raw_df)} raw"
                assert sorted(real["content"]) == sorted(raw_df["Content"]), f"{svc}: line contents differ"
            counts.append(f"{svc}={len(real)}")
        return "real lines equal the LogHub files (count and content): " + ", ".join(counts)

    def incident_set() -> str:
        n = len(world.incidents)
        dev = sum(i["split"] == "dev" for i in world.incidents)
        assert n >= config.N_INCIDENTS * 0.95, f"only {n} incidents (planned {config.N_INCIDENTS})"
        assert abs(dev - round(n * config.DEV_SHARE)) <= 1, f"{dev} dev of {n}"
        ids = [i["id"] for i in world.incidents]
        assert ids == sorted(ids) and len(set(ids)) == n, "incident ids not unique / ordered"
        starts = [i["incident_start"] for i in world.incidents]
        assert starts == sorted(starts), "INC ids not chronological"
        cats = sorted({i.get("category", "?") for i in world.incidents})
        real = sum(i.get("source") == "loghub" for i in world.incidents)
        return (f"{n} incidents: {dev} dev / {n - dev} held out; {real} on real LogHub anomalies; "
                f"{len({i['scenario_type'] for i in world.incidents})} scenario types; categories {', '.join(cats)}")

    def detected() -> str:
        for inc in world.incidents:
            a = ev.get(inc.get("anomaly_id") or "")
            assert a and a["event_type"] == "ANOMALY", f"{inc['id']}: no linked anomaly"
            assert a["service"] == inc["service"], f"{inc['id']}: anomaly on {a['service']}, alert on {inc['service']}"
            assert a["occurred_at"] == inc["incident_start"], f"{inc['id']}: start differs from the anomaly"
        return "every incident starts at an ANOMALY the detector found on its alerting component"

    def topology() -> str:
        for s_, deps in config.TOPOLOGY_CALLS.items():
            assert s_ in config.COMPONENTS and all(d in config.COMPONENTS for d in deps), f"unknown component near {s_}"
        linked = {s_ for s_, d in config.TOPOLOGY_CALLS.items() if d} | {d for ds in config.TOPOLOGY_CALLS.values()
                                                                         for d in ds} | set(config.TOPOLOGY_HOSTS.values())
        assert linked >= set(config.SERVICES), f"unconnected: {set(config.SERVICES) - linked}"
        for inc in world.incidents:
            cs = inc.get("cause_service") or inc["service"]
            assert config.dependency_path(inc["service"], cs), f"{inc['id']}: {cs} unreachable from {inc['service']}"
        hops = [len(config.dependency_path(i["service"], i.get("cause_service") or i["service"])) - 1
                for i in world.incidents]
        return (f"{len(config.SERVICES)} components, all connected; every cause reachable from its alert "
                f"(0 hops: {hops.count(0)}, 1 hop: {hops.count(1)}, 2+ hops: {sum(h >= 2 for h in hops)})")

    def sources() -> str:
        bad = [e["id"] for e in world.events if e["source"] != ("logs" if e["event_type"] in config.LOG_TYPES else "simulated")]
        assert not bad, f"wrong source on {bad[:5]}"
        return f"{len(world.log_events())} log-derived, {len(world.sim_events())} simulated events"

    def ids() -> str:
        nums = [int(e["id"].split("_")[1]) for e in world.events]
        assert len(nums) == len(set(nums)), "duplicate ids"
        order = sorted(world.events, key=lambda e: int(e["id"].split("_")[1]))
        times = [_t(e["occurred_at"]) for e in order]
        assert times == sorted(times), "evt_N not chronological"
        return f"{len(nums)} unique evt ids, chronological"

    def incidents() -> str:
        n = 0
        for inc in world.incidents:
            start = _t(inc["incident_start"])
            cause = inc.get("cause_event_id")
            if inc.get("category") == "external" or inc["scenario_type"] == "NO_CHANGE":
                assert cause is None and inc.get("culprit_change_id") is None, f"{inc['id']}: external with a cause"
                continue
            assert inc.get("mitigated_at", "") > inc["incident_start"], f"{inc['id']}: mitigated before it started"
            assert cause and _t(ev[cause]["occurred_at"]) < start, f"{inc['id']}: cause not before the incident"
            for d in inc["decoy_ids"]:
                assert _t(ev[cause]["occurred_at"]) < _t(ev[d]["occurred_at"]) < start, f"{inc['id']}: decoy {d} out of range"
            for x in inc.get("contributing_ids", []):
                assert _t(ev[x]["occurred_at"]) < start, f"{inc['id']}: contributing {x} after the incident"
            n += 1
        return f"{n} caused incidents + {len(world.incidents) - n} with no causing change; causes precede, decoys closer"

    def culprits() -> str:
        cds = {c_["id"]: c_ for c_ in world.change_details}
        n = 0
        for inc in world.incidents:
            if not inc.get("cause_event_id"):
                continue
            cid = inc.get("culprit_change_id")
            assert cid in cds, f"{inc['id']}: missing breaking line"
            diff = cds[cid].get("diff") or ""
            assert any(x.startswith("+") for x in diff.splitlines()) or any(x.startswith("-") for x in diff.splitlines()), \
                f"{inc['id']}: empty diff"
            row = store.get_change_detail(cid)
            assert row and row["diff"] == diff, f"{inc['id']}: store diff differs"
            if inc["scenario_type"] == "FLAG_DORMANT":
                assert cds[cid]["event_id"] != inc["cause_event_id"], "dormant culprit should be in an earlier deploy"
            n += 1
        return f"{n} breaking lines with real diffs, identical in the store"

    def no_noop() -> str:
        cds = {c_["id"]: c_ for c_ in world.change_details}
        for inc in world.incidents:
            cid = inc.get("culprit_change_id")
            if cid and cds[cid].get("param_key"):
                assert cds[cid]["param_old"] != cds[cid]["param_new"], f"{inc['id']}: no-op parameter cause"
        return "every parameter cause changes its value"

    def param_chain() -> str:
        last: dict[tuple[str, str], str] = {}
        rows = sorted((c_ for c_ in world.change_details if c_.get("param_key")),
                      key=lambda c_: (_t(ev[c_["event_id"]]["occurred_at"]), c_["id"]))
        for c_ in rows:
            k = (ev[c_["event_id"]]["service"], c_["param_key"])
            if k in last:
                assert c_["param_old"] == last[k], f"{k}: {c_['id']} old {c_['param_old']} != previous new {last[k]}"
            last[k] = c_["param_new"]
        return f"{len(rows)} parameter changes chain correctly"

    def versions() -> str:
        cur: dict[str, tuple[int, ...]] = {}
        for e in sorted(world.events, key=lambda e: e["occurred_at"]):
            if e["event_type"] == "DEPLOY" and e["environment"] == "prod":
                v = tuple(int(x) for x in e["version_to"].lstrip("v").split("."))
                assert v > cur.get(e["service"], (0,)), f"{e['id']}: prod version went down to {e['version_to']}"
                cur[e["service"]] = v
        return "prod deploy versions strictly increase per service"

    def post_deploy() -> str:
        from ff.ingest.simulator import load_logs_for_replay, post_deploy_line

        logs = load_logs_for_replay(paths)
        n = covered = 0
        for e in world.events:
            if e["event_type"] in ("DEPLOY", "PATCH", "ROLLBACK") and e["environment"] == "prod":
                n += 1
                line = e["payload"]["post_deploy"]
                assert line == post_deploy_line(e, logs), f"{e['id']}: post-deploy line does not match the logs"
                covered += not line.startswith("no log coverage")
        return f"{n} prod deploys: post-deploy error rates recomputed from the log lines ({covered} with coverage)"

    def descriptions() -> str:
        import re

        n = 0
        for e in world.sim_events():
            if e["event_type"] in config.CHANGE_TYPES and e["environment"] == "prod":
                p = e["payload"]
                assert p.get("description") and p.get("change_request", {}).get("id"), f"{e['id']}: no description"
                assert not re.search(r"\b(evt|chg)_\d+", p["description"]), f"{e['id']}: description cites ids"
                n += 1
        return f"{n} prod changes carry a change request and a natural-language description (no ids in the text)"

    def stores() -> str:
        n_sql, n_vec = len(store.get_events()), vectors.count()["events"]
        assert n_sql == n_vec == len(world.events), f"sqlite {n_sql}, chroma {n_vec}, world {len(world.events)}"
        for svc in config.SERVICES:
            assert store.baseline_count(svc) == 1, f"{svc}: {store.baseline_count(svc)} baselines"
        return f"{n_sql} events in SQLite and Chroma; one baseline per component ({len(config.SERVICES)})"

    def micro() -> str:
        from ff.eval.micro_check import check
        from ff.retrieve.ranker import Ranker

        df = check(Ranker(store, vectors), world.incidents)
        top1, top3 = df["top1"].mean(), df["top3"].mean()
        assert top3 >= 0.8, f"breaking line in the top 3 only {top3:.0%}"
        return f"breaking line ranked first {top1:.0%}, top 3 {top3:.0%} ({len(df)} incidents)"

    def rca_text() -> str:
        for inc in world.incidents:
            for k, v in (inc.get("rca") or {}).items():
                assert "{" not in v, f"{inc['id']}: unfilled template in {k}: {v[:60]}"
        return "macro / micro / remediation texts filled for every incident"

    def streaming_llm() -> str:
        import torch

        from ff.llm.streaming import AnchoredStreamingCache, StreamingCache, forward, pos_shift, prefill
        from ff.llm.tiny import tiny_llama

        model = tiny_llama()
        ids = torch.randint(3, 250, (150,), generator=torch.Generator().manual_seed(0)).tolist()
        ref, got, past = [], [], None
        with pos_shift(model, False):
            for t in ids:
                lg, past = forward(model, [t], past)
                ref.append(lg[0, -1])
        pol, past = StreamingCache(4, 1000), None
        with pos_shift(model, True):
            for t in ids:
                past = pol.make_room(past, 1)
                lg, past = forward(model, [t], past)
                got.append(lg[0, -1])
        diff = float((torch.stack(got) - torch.stack(ref)).abs().max())
        assert diff < 1e-4, f"pos-shift differs from unpatched by {diff}"
        cache = AnchoredStreamingCache(model)
        p0 = cache.init_session({"system": [1] + ids[:60], "initial_query": ids[60:90], "ledger": ids[90:100]})
        start = cache.start_size
        before = [layer[0][:, :, :start].clone() for layer in p0]
        with pos_shift(model, True):
            _, p1 = prefill(model, ids * 12, p0, cache)
        assert all(torch.equal(a, layer[0][:, :, :start]) for a, layer in zip(before, p1)), "anchor keys changed"
        return f"pos-shift == unpatched (max diff {diff:.1e}); anchor keys unchanged after {len(ids) * 12} tokens"

    def knowledge_base() -> str:

        from ff.ingest import repo
        from ff.ingest.doc_indexer import doc_published

        kb = paths.docs_arch
        days: dict[str, str] = {}
        n_prod = 0
        for e in world.sim_events():
            if e["event_type"] in config.CHANGE_TYPES and e["environment"] == "prod":
                day = e["occurred_at"][:10]
                if day not in days:
                    days[day] = (kb / "changelog" / f"{day}.md").read_text(encoding="utf-8")
                assert f"[{e['id']}]" in days[day], f"{day}: prod change {e['id']} missing from the change log"
                assert doc_published(days[day]) >= _t(e["occurred_at"]), f"{day}: change log published too early"
                n_prod += 1
        plans = list((kb / "releases").glob("*.md"))
        assert plans, "no release-train plans"
        for svc in config.SERVICES:
            text = (kb / "codebase" / f"{svc}.md").read_text(encoding="utf-8")
            for f, _, _ in repo.CODE_FILES.get(svc, []):
                assert f"`{f}`" in text, f"codebase guide {svc} misses {f}"
        pms = list((kb / "postmortems").glob("*.md"))
        assert pms, "no postmortems"
        by_inc = {i["id"]: i for i in world.incidents}
        n_inc = 0
        for pm in pms:
            text = pm.read_text(encoding="utf-8")
            inc_id = pm.stem.split("__")[-1]
            if inc_id in by_inc:
                assert doc_published(text) > _t(by_inc[inc_id]["mitigated_at"]), f"{pm.name} published before the fix"
                n_inc += 1
            else:
                assert doc_published(text) < config.DAY0, f"{pm.name}: past postmortem inside the history"
        chunks = vectors.architecture.count()
        assert chunks > 0, "knowledge base not indexed"
        return (f"{len(plans)} release-train plans; {len(days)} daily change logs cover all {n_prod} prod changes; "
                f"codebase guides list every file; {n_inc} incident postmortems published after their fix + "
                f"{len(pms) - n_inc} older ones; {chunks} chunks indexed")

    def train_order() -> str:
        from ff.ingest import cycles

        n = 0
        by_key: dict[str, list[dict[str, Any]]] = {}
        for e in world.sim_events():
            if e["event_type"] == "DEPLOY" and e["environment"] == "prod" and e["_sim"].get("role") == "background":
                for f in e["payload"].get("features", []):
                    by_key.setdefault(f"{e['payload']['cycle']}|{f['ticket']}", []).append(e)
        for k, evs in by_key.items():
            evs.sort(key=lambda e: e["occurred_at"])
            levels = [cycles.LEVEL[e["service"]] for e in evs]
            assert levels == sorted(levels), f"{k}: deploy order {[e['service'] for e in evs]} is not callee-first"
            n += len(evs) > 1
        return f"{n} multi-service features deploy callee-first (hosts / stores -> leaf services -> callers -> edge)"

    def cross_service() -> str:
        n = far = 0
        for inc in world.incidents:
            if inc["scenario_type"] in ("CROSS_SERVICE", "CONTRACT_BREAK"):
                cs = inc["cause_service"]
                assert cs != inc["service"], f"{inc['id']}: cause on the alerting component"
                assert config.dependency_path(inc["service"], cs), f"{inc['id']}: cause not reachable"
                far += cs not in config.neighbourhood(inc["service"])
                n += 1
        return f"{n} cross-service incidents: cause never on the alerting component; {far} need a lens hop"

    for name, fn in (("real logs untouched", logs_untouched), ("incident set and split", incident_set),
                     ("incidents detected in the logs", detected), ("topology and paths", topology),
                     ("event sources", sources), ("id numbering", ids),
                     ("incident ground truth", incidents), ("breaking lines", culprits), ("no no-op causes", no_noop),
                     ("parameter chains", param_chain), ("prod versions", versions), ("post-deploy lines", post_deploy),
                     ("change descriptions", descriptions),
                     ("stores", stores), ("suspect-line ranking", micro), ("RCA templates", rca_text),
                     ("knowledge base", knowledge_base), ("release-train deploy order", train_order),
                     ("cross-service incidents", cross_service),
                     ("StreamingLLM (tiny model)", streaming_llm)):
        c.run(name, fn)
    if smoke:
        def real_smoke() -> str:
            from ff.eval.smoke import run as smoke_run

            r = smoke_run()
            assert r["patch_ok"], "streaming not better than window"
            return (f"streaming ppl {r['ppl_streaming_4_1020']:.2f} < window ppl {r['ppl_window_1024']:.2f}; "
                    f"distinct-2 {r['distinct2']:.2f}")
        c.run("real-model smoke", real_smoke)
    paths.results.mkdir(parents=True, exist_ok=True)
    (paths.results / "verify_report.md").write_text("# FaultFalcon verification\n\n" + c.table() + "\n", encoding="utf-8")
    return c


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--smoke", action="store_true", help="also run the real-model smoke check (~20 min on CPU)")
    args = ap.parse_args(argv)
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    c = run(smoke=args.smoke)
    width = max(len(n) for n, _, _ in c.rows)
    for n, ok, d in c.rows:
        print(f"{'PASS' if ok else 'FAIL'}  {n:{width}s}  {d}")
    print(f"\n{'ALL CHECKS PASSED' if c.ok else 'SOME CHECKS FAILED'} -> results/verify_report.md")
    sys.exit(0 if c.ok else 1)


if __name__ == "__main__":
    main()


