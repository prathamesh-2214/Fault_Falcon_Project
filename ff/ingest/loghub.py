"""LogHub 2k samples: download, parse into one schema, audit, and rescale onto the timeline.

The logs are never modified: parsing only reads the structured CSVs, and the timeline
rescale stores the original timestamp next to the new one (``original_ts`` / ``ts``).

Unified schema (one DataFrame per system)::

    line_id, original_ts, ts, level, component, content, event_id, event_template,
    label (BGL/Thunderbird only, '-' = normal; None elsewhere), is_error,
    http_status, latency_s   (OpenStack only; NaN elsewhere)

Run ``python -m ff.ingest.loghub`` (``make data``) to download, parse and audit.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from ff import config
from ff.config import Paths, get_paths

_OPENSTACK_HTTP = re.compile(r'"(?:GET|POST|PUT|DELETE|PATCH|HEAD) [^"]*"\s+status:\s*(\d+)\s+len:\s*\d+\s+time:\s*([\d.]+)')
_LEVEL_ALIASES = {"WARNING": "WARN", "SEVERE": "SEVERE", "FAILURE": "FATAL"}


# --------------------------------------------------------------------------- download
def file_urls(system: str) -> dict[str, str]:
    """Raw GitHub URLs of the three 2k files for ``system``."""
    return {f"{system}{sfx}": f"{config.LOGHUB_BASE_URL}/{system}/{system}{sfx}" for sfx in config.LOGHUB_SUFFIXES}


def download(systems: tuple[str, ...] = config.LOGHUB_SYSTEMS, paths: Paths | None = None,
             timeout: float = 60.0) -> list[Path]:
    """Download the 2k samples into ``data/raw/loghub/<System>/``; skip files already present."""
    import requests

    paths = (paths or get_paths()).ensure()
    written: list[Path] = []
    for system in systems:
        folder = paths.raw_loghub / system
        folder.mkdir(parents=True, exist_ok=True)
        for name, url in file_urls(system).items():
            dest = folder / name
            if dest.exists() and dest.stat().st_size > 0:
                continue
            r = requests.get(url, timeout=timeout)
            r.raise_for_status()
            dest.write_bytes(r.content)
            written.append(dest)
    return written


# --------------------------------------------------------------------------- parsing
def _parse_ts(system: str, df: pd.DataFrame) -> pd.Series:
    """Build UTC timestamps from each system's own Date/Time columns."""
    if system == "OpenStack":
        s, fmt = df["Date"] + " " + df["Time"], "%Y-%m-%d %H:%M:%S.%f"
    elif system in ("Hadoop", "Zookeeper"):
        s, fmt = df["Date"] + " " + df["Time"].str.replace(",", ".", regex=False), "%Y-%m-%d %H:%M:%S.%f"
    elif system == "HDFS":
        s, fmt = df["Date"].str.zfill(6) + " " + df["Time"].str.zfill(6), "%y%m%d %H%M%S"
    elif system == "BGL":
        s, fmt = df["Time"], "%Y-%m-%d-%H.%M.%S.%f"
    elif system == "Thunderbird":
        s, fmt = df["Date"] + " " + df["Time"], "%Y.%m.%d %H:%M:%S"
    else:
        raise ValueError(f"no timestamp rule for {system}")
    return pd.to_datetime(s, format=fmt, utc=True)


def _level(system: str, df: pd.DataFrame) -> pd.Series:
    if "Level" not in df.columns:  # Thunderbird has no level column; do not invent one
        return pd.Series("NA", index=df.index)
    lv = df["Level"].fillna("NA").astype(str).str.strip().str.upper()
    return lv.map(lambda x: _LEVEL_ALIASES.get(x, x))


def parse_structured(system: str, path: Path) -> pd.DataFrame:
    """Parse ``<System>_2k.log_structured.csv`` into the unified schema (sorted by time)."""
    raw = pd.read_csv(path, dtype=str, keep_default_na=False)
    df = pd.DataFrame({
        "line_id": raw["LineId"].astype(int),
        "original_ts": _parse_ts(system, raw),
        "level": _level(system, raw),
        "component": raw.get("Component", pd.Series("", index=raw.index)).astype(str),
        "content": raw["Content"].astype(str),
        "event_id": raw["EventId"].astype(str),
        "event_template": raw["EventTemplate"].astype(str),
    })
    df["label"] = raw["Label"].astype(str) if system in config.LABELLED_SYSTEMS else None
    df["is_error"] = ~df["level"].isin(config.INFO_LEVELS)
    df["http_status"] = np.nan
    df["latency_s"] = np.nan
    if system == "OpenStack":
        m = df["content"].str.extract(_OPENSTACK_HTTP)
        df["http_status"] = pd.to_numeric(m[0], errors="coerce")
        df["latency_s"] = pd.to_numeric(m[1], errors="coerce")
    df = df.sort_values(["original_ts", "line_id"], kind="stable").reset_index(drop=True)
    df["system"] = system
    return df


def load_system(system: str, paths: Paths | None = None) -> pd.DataFrame:
    """Parse one downloaded system."""
    paths = paths or get_paths()
    return parse_structured(system, paths.raw_loghub / system / f"{system}_2k.log_structured.csv")


# --------------------------------------------------------------------------- timeline
def rescale(df: pd.DataFrame, start: datetime = config.LOG_START,
            hours: float = config.LOG_WINDOW_HOURS) -> pd.DataFrame:
    """Order-preserving linear map of ``original_ts`` onto ``[start, start + hours]``.

    Relative gaps are kept. If all timestamps are equal, lines are spread evenly.
    """
    out = df.sort_values(["original_ts", "line_id"], kind="stable").reset_index(drop=True).copy()
    t = out["original_ts"].astype("int64").to_numpy(dtype=np.float64)
    span = t.max() - t.min() if len(t) else 0.0
    frac = (t - t.min()) / span if span > 0 else np.linspace(0.0, 1.0, len(t))
    offset_ns = (frac * hours * 3600 * 1e9).astype("int64")
    out["ts"] = pd.Timestamp(start) + pd.to_timedelta(offset_ns, unit="ns")
    return out


def load_service_logs(paths: Paths | None = None) -> dict[str, pd.DataFrame]:
    """service -> parsed + rescaled log DataFrame, per ``SERVICE_MAP``."""
    out = {}
    for svc, system in config.SERVICE_MAP.items():
        df = rescale(load_system(system, paths))
        df["service"] = svc
        out[svc] = df
    return out


# --------------------------------------------------------------------------- audit
def bursts(df: pd.DataFrame, window: int = 10, min_errors: int = 5) -> int:
    """Number of 10-line rolling windows with at least 5 non-INFO lines."""
    if len(df) < window:
        return 0
    return int((df["is_error"].astype(int).rolling(window).sum() >= min_errors).sum())


def audit_system(system: str, df: pd.DataFrame) -> str:
    """Markdown audit section for one system."""
    lines = [f"## {system}", ""]
    t0, t1 = df["original_ts"].min(), df["original_ts"].max()
    lines.append(f"- lines: {len(df)}")
    lines.append(f"- time span: {t0} -> {t1} ({(t1 - t0)})")
    lc = df["level"].value_counts().to_dict()
    lines.append(f"- levels: {lc}")
    if df["label"].notna().any():
        lab = df["label"].value_counts()
        lines.append(f"- labels: normal '-'={int(lab.get('-', 0))}, alerts={int(lab.drop('-', errors='ignore').sum())} "
                     f"{lab.drop('-', errors='ignore').head(8).to_dict()}")
    lines.append(f"- distinct templates: {df['event_template'].nunique()}")
    if system == "OpenStack":
        http = df["http_status"].notna()
        lines.append(f"- HTTP lines with parsed latency: {int(df.loc[http, 'latency_s'].notna().sum())}/{int(http.sum())}; "
                     f"5xx: {int((df['http_status'] >= 500).sum())}; p95 latency {df['latency_s'].quantile(0.95):.3f}s")
    lines.append(f"- bursts (10-line windows with >=5 non-INFO): {bursts(df)}")
    top = df[df["is_error"]]["event_template"].value_counts().head(10)
    lines.append("- top non-INFO templates:" if len(top) else "- top non-INFO templates: none")
    for tpl, n in top.items():
        lines.append(f"  - {n} x `{tpl[:140]}`")
    lines.append("")
    return "\n".join(lines)


def audit(paths: Paths | None = None, systems: tuple[str, ...] = config.LOGHUB_SYSTEMS) -> str:
    """Write ``data/audit.md`` for all downloaded systems and return it."""
    paths = (paths or get_paths()).ensure()
    parts = ["# LogHub audit", "", f"Service map: {config.SERVICE_MAP}", ""]
    for system in systems:
        f = paths.raw_loghub / system / f"{system}_2k.log_structured.csv"
        if f.exists():
            parts.append(audit_system(system, parse_structured(system, f)))
    text = "\n".join(parts)
    (paths.data / "audit.md").write_text(text, encoding="utf-8")
    return text


def main(argv: list[str] | None = None) -> None:
    """CLI: download (unless --offline), then print the audit."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--offline", action="store_true", help="skip downloading; use files already present")
    args = ap.parse_args(argv)
    paths = get_paths().ensure()
    if not args.offline:
        got = download(paths=paths)
        print(f"downloaded {len(got)} files into {paths.raw_loghub}")
    print(audit(paths))
    print(f"log window: {config.LOG_START} -> {config.SIM_END} ({timedelta(hours=config.LOG_WINDOW_HOURS)})")


if __name__ == "__main__":
    main()
