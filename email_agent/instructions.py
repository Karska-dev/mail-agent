"""Turn the user's plain-English answer into a list of actions (offline parser).

"Save the coupon code and its conditions to my notes, then delete the email."
  -> [{"type": "save_note", "fields": ["coupon_codes", "conditions", "expiry"]},
      {"type": "delete"}]
"""
from __future__ import annotations

import re
from typing import Any

from .models import Analysis

SKIP_RE = re.compile(r"^\s*(skip|later|not sure|don'?t know|idk|pass|ask me later)\b", re.I)

ACTION_PATTERNS: list[tuple[str, str]] = [
    ("show", r"\b(?:make|let) me (?:read|see|view|look)|\bi (?:want|need|would like|'d like) to (?:read|see|view|look at)"
             r"|\bshow (?:it |them )?(?:to )?me\b|\b(?:read|view|see) it (?:first|myself)|\bfor me to read"),
    ("unsubscribe", r"unsubscribe|opt[ -]?out|stop (?:these|them|getting|receiving|sending)"),
    ("archive", r"\barchiv\w*|\bfile (?:it|this|the email)|keep (?:it |this )?for (?:later|reference)"),
    ("delete", r"\bdelete|\btrash|\bbin\b|\bdiscard|get rid of|\bremove (?:it|this|the email)"),
    ("add_todo", r"to-?\s?do|\btasks?\b|\bremind(?:er)?\b"),
    ("save_note", r"\bnotes?\b|write (?:it )?down|\bjot\b|\brecord\b"),
    ("keep", r"leave it|keep (?:it )?in (?:my |the )?inbox|mark (?:it )?(?:as )?important|\bflag\b|\bstar\b"),
]
SAVE_RE = re.compile(r"\bsave\b|\bstore\b|\bkeep (?:the|track)", re.I)
NEGATION_RE = re.compile(r"\b(don'?t|do not|never|no need to|without)\s+(?:\w+\s+){0,2}$", re.I)

FIELD_PATTERNS: list[tuple[list[str], str]] = [
    (["coupon_codes"], r"coupon|promo|discount|\bcodes?\b|voucher"),
    (["conditions", "expiry"], r"condition|terms|restriction|valid|minimum|exclusion|fine print"),
    (["expiry"], r"expir|valid until|end date"),
    (["deadline"], r"due date|deadline|\bdue\b|by when"),
    (["event"], r"appointment|meeting|\bevent\b|reservation|booking|\btime\b"),
    (["deadline", "event", "expiry", "dates"], r"(?<!due )\bdates?\b|\bwhen\b"),
    (["amount_due"], r"amount|how much|\btotal\b|balance|\bprice\b|\bcost\b"),
    (["reference_numbers"], r"reference|order (?:number|#|id)|invoice|account|confirmation|\bref\b|\bnumber\b"),
    (["tracking_numbers"], r"tracking"),
    (["links"], r"\blinks?\b|\burl\b"),
    (["action_items"], r"what (?:i|to) (?:need to )?do|action items?|next steps"),
]


def _find_fields(text: str) -> list[str]:
    fields: list[str] = []
    for names, pat in FIELD_PATTERNS:
        if re.search(pat, text, re.I):
            fields += [n for n in names if n not in fields]
    return fields


def _positions(text: str) -> list[tuple[int, str]]:
    found: dict[str, int] = {}
    for kind, pat in ACTION_PATTERNS:
        for m in re.finditer(pat, text, re.I):
            if NEGATION_RE.search(text[: m.start()]):
                continue
            found.setdefault(kind, m.start())
            break
    if "save_note" not in found and "add_todo" not in found:
        m = SAVE_RE.search(text)
        if m:
            found["save_note"] = m.start()
    return sorted((pos, kind) for kind, pos in found.items())


class HeuristicInstructionParser:
    name = "offline"

    def parse(self, text: str, analysis: Analysis) -> list[dict[str, Any]] | None:
        """Returns [] for 'skip', None if nothing was understood."""
        if SKIP_RE.search(text):
            return []
        ordered = _positions(text)
        if not ordered:
            return None
        clauses = [c for c in re.split(r",|;|\.\s|\bthen\b|\balso\b", text, flags=re.I) if c.strip()]
        actions: list[dict[str, Any]] = []
        for _, kind in ordered:
            action: dict[str, Any] = {"type": kind}
            if kind in ("save_note", "add_todo"):
                pat = dict(ACTION_PATTERNS).get(kind)
                own = [c for c in clauses if re.search(pat, c, re.I)] if pat else []
                if kind == "save_note" and not own:
                    own = [c for c in clauses if SAVE_RE.search(c)]
                fields = _find_fields(" ".join(own)) if own else []
                if not fields and kind == "save_note":
                    fields = _find_fields(text) if len(ordered) <= 2 else []
                action["fields"] = fields
            actions.append(action)
        return actions
