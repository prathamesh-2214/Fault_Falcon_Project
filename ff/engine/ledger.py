"""The investigation ledger: CONFIRMED / RULED_OUT / OPEN / NEXT_CHECK lines citing ids.

The UI buttons (Confirm / Refute / Mark ruled out) are the source of truth: a 1.1B model
cannot be trusted to maintain a structured block. The model's own "RULED OUT:" /
"NEXT CHECK:" lines only become OPEN *suggestions* that the engineer can accept, and only
if they cite ids that were in the context.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from typing import Any

from ff import config

KINDS = ("CONFIRMED", "CONTRIBUTING", "RULED_OUT", "OPEN", "NEXT_CHECK")
_IDS = re.compile(r"\[((?:evt|chg)_\d+)\]")
_SUGGEST = re.compile(r"^\s*(RULED OUT|NEXT CHECK)\s*:\s*(.+)$", re.IGNORECASE | re.MULTILINE)


@dataclass
class LedgerLine:
    kind: str
    text: str
    ids: list[str] = field(default_factory=list)

    def render(self) -> str:
        cites = " ".join(f"[{i}]" for i in self.ids)
        body = self.text if all(f"[{i}]" in self.text for i in self.ids) else f"{cites} {self.text}".strip()
        return f"{self.kind}: {body}"


class Ledger:
    """Ordered ledger lines + pending model suggestions."""

    def __init__(self, lines: Iterable[LedgerLine] = ()) -> None:
        self.lines: list[LedgerLine] = list(lines)
        self.suggestions: list[LedgerLine] = []

    # ------------------------------------------------------------------ primary (buttons)
    def _set(self, kind: str, eid: str, text: str) -> LedgerLine:
        self.lines = [ln for ln in self.lines
                      if not (ln.ids == [eid] and ln.kind in ("CONFIRMED", "CONTRIBUTING", "RULED_OUT", "OPEN"))]
        line = LedgerLine(kind, text, [eid])
        self.lines.append(line)
        return line

    def confirm(self, eid: str, note: str = "") -> LedgerLine:
        return self._set("CONFIRMED", eid, note or "confirmed by engineer")

    def contributing(self, eid: str, note: str = "") -> LedgerLine:
        """A change that made the incident worse but is not the root cause."""
        return self._set("CONTRIBUTING", eid, note or "contributing factor")

    def refute(self, eid: str, note: str = "") -> LedgerLine:
        return self._set("RULED_OUT", eid, note or "refuted by engineer")

    def rule_out(self, eid: str, note: str = "") -> LedgerLine:
        return self._set("RULED_OUT", eid, note or "ruled out by engineer")

    def add_open(self, text: str, ids: Iterable[str] = ()) -> LedgerLine:
        line = LedgerLine("OPEN", text, list(ids))
        self.lines.append(line)
        return line

    # ------------------------------------------------------------------ secondary (model)
    def suggestions_from_answer(self, answer: str, context_ids: Iterable[str]) -> list[LedgerLine]:
        """OPEN suggestions from 'RULED OUT:' / 'NEXT CHECK:' lines citing ids in the context."""
        allowed = set(context_ids)
        new = []
        for m in _SUGGEST.finditer(answer):
            ids = [i for i in _IDS.findall(m.group(2)) if i in allowed]
            if ids:
                s = LedgerLine("OPEN", f"{m.group(1).upper()}: {m.group(2).strip()}", ids)
                if all(s.text != x.text for x in self.suggestions):
                    self.suggestions.append(s)
                    new.append(s)
        return new

    def accept_suggestion(self, index: int) -> LedgerLine:
        line = self.suggestions.pop(index)
        self.lines.append(line)
        return line

    # ------------------------------------------------------------------ views
    def cited_ids(self) -> list[str]:
        return list(dict.fromkeys(i for ln in self.lines for i in ln.ids))

    def ids_of(self, kind: str) -> list[str]:
        return [i for ln in self.lines if ln.kind == kind for i in ln.ids]

    def render(self, count: Callable[[str], int] | None = None, max_tokens: int = config.LEDGER_MAX_TOKENS) -> str:
        """Plain-text ledger, capped at ``max_tokens``: old CONFIRMED lines merge first, then the
        oldest OPEN / NEXT_CHECK lines are dropped."""
        count = count or (lambda s: len(s.split()))
        lines = list(self.lines)

        def text(ls: list[LedgerLine]) -> str:
            body = "\n".join(ln.render() for ln in ls) if ls else "(empty)"
            return "Investigation ledger:\n" + body

        if count(text(lines)) > max_tokens:
            confirmed = [ln for ln in lines if ln.kind == "CONFIRMED"]
            if len(confirmed) > 1:
                merged = LedgerLine("CONFIRMED", "(merged)", [i for ln in confirmed for i in ln.ids])
                lines = [merged] + [ln for ln in lines if ln.kind != "CONFIRMED"]
        while count(text(lines)) > max_tokens and any(ln.kind in ("OPEN", "NEXT_CHECK") for ln in lines):
            idx = next(i for i, ln in enumerate(lines) if ln.kind in ("OPEN", "NEXT_CHECK"))
            lines.pop(idx)
        while count(text(lines)) > max_tokens and len(lines) > 1:
            lines.pop(0)
        return text(lines)

    def to_list(self) -> list[dict[str, Any]]:
        return [asdict(ln) for ln in self.lines]

    @classmethod
    def from_list(cls, rows: Iterable[dict[str, Any]]) -> Ledger:
        return cls(LedgerLine(r["kind"], r["text"], list(r.get("ids", []))) for r in rows)
