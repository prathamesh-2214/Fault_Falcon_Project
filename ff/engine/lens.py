"""The investigation lens and the engineer's redirect commands.

Commands (typed in chat or sent by UI buttons)::

    lens storage-svc      focus evt_123      stage runtime      pin evt_77      unpin evt_12
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from ff import config

SERVICE_ALIASES: dict[str, str] = {
    "cloud-api": "cloud-api", "cloud api": "cloud-api", "api": "cloud-api", "openstack": "cloud-api",
    "compute-svc": "compute-svc", "compute": "compute-svc", "hadoop": "compute-svc",
    "storage-svc": "storage-svc", "storage": "storage-svc", "hdfs": "storage-svc",
    "coord-svc": "coord-svc", "coord": "coord-svc", "zookeeper": "coord-svc",
    "node-svc": "node-svc", "node": "node-svc", "bgl": "node-svc",
}
_CMD = re.compile(r"^\s*(lens|focus|stage|pin|unpin)\s+([\w.\-]+)\s*$", re.IGNORECASE)
_TIME = re.compile(r"\b(?:since|at|from|around)\s+(\d{1,2}):(\d{2})\b", re.IGNORECASE)


@dataclass
class Lens:
    """What the investigation is looking at."""

    service: str
    incident_start: datetime
    environment: str = "prod"
    window_days: int = config.WINDOW_DAYS
    stage: str = "CHANGE_TIMELINE"
    focus: str | None = None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["incident_start"] = self.incident_start.isoformat()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Lens:
        d = dict(d)
        if isinstance(d.get("incident_start"), str):
            d["incident_start"] = datetime.fromisoformat(d["incident_start"])
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})

    @property
    def window_start(self) -> datetime:
        return self.incident_start - timedelta(days=self.window_days)

    @property
    def window_end(self) -> datetime:
        return self.incident_start + timedelta(hours=config.AFTER_INCIDENT_HOURS)

    def render(self, include_stage: bool = False) -> str:
        """One line for the lens_baseline segment.

        The stage is left out by default: it travels in each user turn, so a stage jump
        does not force a start-region rebuild.
        """
        s = (f"Lens: {self.service} ({self.environment}), incident start "
             f"{self.incident_start.strftime('%Y-%m-%d %H:%M')} UTC, window {self.window_days} days")
        if include_stage:
            s += f", stage {self.stage}"
        return s + (f", focus [{self.focus}]" if self.focus else "")


@dataclass(frozen=True)
class Command:
    """A parsed redirect command."""

    kind: str  # lens | focus | stage | pin | unpin
    arg: str


def find_service(text: str) -> str | None:
    """First service named in ``text`` (full names first, then aliases)."""
    low = text.lower()
    for svc in config.SERVICES:
        if svc in low:
            return svc
    for alias, svc in sorted(SERVICE_ALIASES.items(), key=lambda kv: -len(kv[0])):
        if re.search(rf"\b{re.escape(alias)}\b", low):
            return svc
    return None


def parse_command(text: str) -> Command | None:
    """Parse a redirect command; None if ``text`` is a normal question."""
    m = _CMD.match(text)
    if not m:
        return None
    kind, arg = m.group(1).lower(), m.group(2)
    if kind == "lens":
        svc = find_service(arg)
        return Command("lens", svc) if svc else None
    if kind == "stage":
        from ff.engine.stages import normalise_stage

        st = normalise_stage(arg)
        return Command("stage", st) if st else None
    if kind in ("focus", "pin", "unpin") and not re.fullmatch(r"(evt|chg)_\d+", arg):
        return None
    return Command(kind, arg)


def apply_command(lens: Lens, cmd: Command) -> Lens:
    """New lens after a lens / focus / stage command (pin/unpin do not change the lens)."""
    if cmd.kind == "lens":
        return replace(lens, service=cmd.arg, focus=None)
    if cmd.kind == "focus":
        return replace(lens, focus=cmd.arg)
    if cmd.kind == "stage":
        return replace(lens, stage=cmd.arg)
    return lens


def parse_initial_query(text: str, default_service: str | None = None,
                        default_start: datetime | None = None) -> Lens:
    """Draft lens from the engineer's first message.

    The service comes from the text (else ``default_service``). The incident start comes
    from ``default_start`` (e.g. the picked incident); a time like "since 08:40" moves it
    to that clock time on the same day.
    """
    svc = find_service(text) or default_service or config.SERVICES[0]
    start = default_start or (config.SIM_END - timedelta(hours=1))
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    m = _TIME.search(text)
    if m and default_start is None:
        start = start.replace(hour=int(m.group(1)) % 24, minute=int(m.group(2)), second=0, microsecond=0)
    return Lens(service=svc, incident_start=start)
