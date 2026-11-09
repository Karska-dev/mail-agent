"""Core data types: emails, analyses, rules and the shared vocabulary."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from email.utils import parseaddr
from typing import Any

# --------------------------------------------------------------------------- #
# Shared vocabulary. The analyzer (Claude or offline) must speak these terms,
# because rules are matched on them.
# --------------------------------------------------------------------------- #
CATEGORIES: dict[str, str] = {
    "promotion": "marketing / sales / ads from a company",
    "newsletter": "editorial content, digests, blogs",
    "bill": "a bill, invoice or statement that needs paying",
    "receipt": "order or payment confirmation",
    "shipping": "shipment / delivery updates",
    "appointment": "appointments, reservations, bookings, events",
    "security": "passwords, sign-in alerts, verification codes",
    "social": "social network notifications",
    "work": "work correspondence",
    "personal": "a personal message from a real person",
    "other": "anything else",
}

FEATURES: dict[str, str] = {
    "has_coupon": "a discount / promo code",
    "has_deadline": "something that must be done by a date",
    "has_event": "an appointment, meeting or reservation time",
    "has_amount_due": "an amount of money you need to pay",
    "has_reference": "an order / invoice / account / confirmation number",
    "has_tracking": "a shipment tracking number",
    "is_bulk": "bulk mailing-list mail",
}

# Features that change what an email *means*. A rule learned on an email
# without one of these will not silently fire on an email that has it.
SIGNIFICANT_FEATURES = ["has_coupon", "has_deadline", "has_event", "has_amount_due", "has_tracking"]

EXTRACT_FIELDS: dict[str, str] = {
    "coupon_codes": "coupon code",
    "conditions": "conditions",
    "expiry": "offer valid until",
    "deadline": "deadline",
    "event": "when",
    "amount_due": "amount due",
    "reference_numbers": "reference numbers",
    "tracking_numbers": "tracking numbers",
    "dates": "dates",
    "action_items": "action items",
    "links": "links",
}

# If an action uses a field, the rule requires the matching feature.
FIELD_FEATURE = {
    "coupon_codes": "has_coupon",
    "conditions": "has_coupon",
    "expiry": "has_coupon",
    "deadline": "has_deadline",
    "event": "has_event",
    "amount_due": "has_amount_due",
    "tracking_numbers": "has_tracking",
    "reference_numbers": "has_reference",
}

ACTION_TYPES = {
    "show": "show it to you to read first",
    "save_note": "save to notes",
    "add_todo": "add to to-do list",
    "unsubscribe": "unsubscribe",
    "archive": "archive",
    "delete": "delete",
    "keep": "leave in inbox",
}
TERMINAL_ACTIONS = {"archive", "delete", "keep"}

_MULTI_PART_SUFFIXES = {"co.uk", "com.au", "co.jp", "co.kr", "com.br", "co.nz", "com.mx", "co.in", "com.sg"}


def org_domain(domain: str) -> str:
    """mail.northwind.example -> northwind.example ; email.shop.co.uk -> shop.co.uk"""
    parts = domain.lower().strip(".").split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in _MULTI_PART_SUFFIXES:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


@dataclass
class Email:
    id: str
    sender: str
    subject: str
    body: str
    date: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    uid: str | None = None  # server-side id for IMAP
    raw: bytes | None = field(default=None, repr=False, compare=False)  # original RFC 822 bytes (IMAP)

    @property
    def sender_address(self) -> str:
        return parseaddr(self.sender)[1].lower()

    @property
    def sender_name(self) -> str:
        return parseaddr(self.sender)[0].strip()

    @property
    def sender_domain(self) -> str:
        addr = self.sender_address
        return org_domain(addr.split("@", 1)[1]) if "@" in addr else addr

    def header(self, name: str) -> str | None:
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Email":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


@dataclass
class Analysis:
    category: str
    summary: str
    sender_label: str
    features: list[str]
    extracted: dict[str, Any]
    confidence: float
    engine: str = "offline"
    importance: str = "normal"          # high | normal | low
    importance_reason: str = ""
    risk: str = "none"                  # phishing risk: none | low | high
    risk_signs: list[str] = field(default_factory=list)
    seconds: float = 0.0                # how long reading it took
    task_title: str = ""                # plain-words to-do, e.g. "Order class photos from Brightframe Studio by Sep 18"
    note_title: str = ""                # plain-words name, e.g. "Friday Folder — Maple Grove School"
    key_link: str = ""                  # the one link that matters (tracking, order form, payment…)
    key_link_label: str = ""

    def has(self, feature: str) -> bool:
        return feature in self.features

    def values(self, fields: list[str] | None = None) -> dict[str, Any]:
        """Non-empty extracted values for the requested fields (all if None)."""
        keys = fields or list(EXTRACT_FIELDS)
        out = {}
        for k in keys:
            v = self.extracted.get(k)
            if v not in (None, "", [], {}):
                out[k] = v
        return out


@dataclass
class Rule:
    id: str
    name: str
    scope: str  # "sender" (one sender's domain) | "any" (any sender)
    sender_domain: str | None
    sender_label: str
    category: str
    required_features: list[str]
    excluded_features: list[str]
    actions: list[dict[str, Any]]
    instruction: str = ""
    created_at: str = ""
    times_applied: int = 0
    last_applied: str | None = None
    derived_from: str | None = None
    unsubscribe_offered: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Rule":
        return cls(**{k: d[k] for k in cls.__dataclass_fields__ if k in d})


def describe_actions(actions: list[dict[str, Any]]) -> str:
    parts = []
    for a in actions:
        label = ACTION_TYPES.get(a["type"], a["type"])
        fields = a.get("fields")
        if fields and a["type"] in ("save_note", "add_todo"):
            label += " (" + ", ".join(EXTRACT_FIELDS.get(f, f) for f in fields) + ")"
        parts.append(label)
    return " → ".join(parts) if parts else "nothing"


def describe_features(features: list[str]) -> str:
    return ", ".join(FEATURES.get(f, f) for f in features)
