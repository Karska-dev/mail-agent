"""Claude-powered analyzer and instruction parser.

Both use forced tool calls so the output is always structured JSON in the
same vocabulary as the offline engine. On any API error they fall back to
the offline engine, so the agent never stalls.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from typing import Any

from .analyzer import RISK_ORDER, HeuristicAnalyzer, assess
from .instructions import SKIP_RE, HeuristicInstructionParser
from .netutil import ssl_context
from .models import ACTION_TYPES, CATEGORIES, EXTRACT_FIELDS, FEATURES, Analysis, Email

DEFAULT_MODEL = os.environ.get("EMAIL_AGENT_MODEL", "claude-sonnet-5")

_str_list = {"type": "array", "items": {"type": "string"}}
_nullable_str = {"type": ["string", "null"]}

ANALYSIS_TOOL = {
    "name": "record_analysis",
    "description": "Record the structured analysis of one email.",
    "input_schema": {
        "type": "object",
        "properties": {
            "category": {"type": "string", "enum": list(CATEGORIES)},
            "summary": {"type": "string", "description": "One sentence: what this email is and what it wants."},
            "sender_label": {"type": "string", "description": "Short brand or person name, e.g. 'Northwind'."},
            "features": {"type": "array", "items": {"type": "string", "enum": list(FEATURES)}},
            "confidence": {"type": "number", "description": "0-1: how sure you are of category and features."},
            "importance": {"type": "string", "enum": ["high", "normal", "low"],
                           "description": "high = the person should really see it (security, money, deadlines, "
                                          "real people, appointments); low = marketing/newsletters."},
            "importance_reason": {"type": "string", "description": "A few words: why it matters, or empty."},
            "task_title": {"type": "string", "description": (
                "If the person needs to do something: a short to-do in plain words - verb + what + for whom / "
                "which organisation + date if there is one, e.g. 'Order class photos from Brightframe Studio by Sep 18'. "
                "Never copy button text like 'Shop now' or 'Download the app'. Empty if nothing to do.")},
            "note_title": {"type": "string", "description": (
                "A short name for this email if saved as a note: what it is + who it's from, e.g. "
                "'Friday Folder - Maple Grove School', 'Craft kit order shipped', 'Metro Power bill, September'.")},
            "key_link": {"type": "integer", "description": (
                "Number N of the single most useful [link N] in the email (tracking, order form, payment, "
                "sign-up), or 0 if none.")},
            "key_link_label": {"type": "string", "description": "What that link does, e.g. 'Track package'."},
            "phishing_risk": {"type": "string", "enum": ["none", "low", "high"],
                              "description": "Signs this could be a fake/scam email."},
            "phishing_signs": {"type": "array", "items": {"type": "string"},
                               "description": "Concrete warning signs, short, in plain language."},
            "extracted": {
                "type": "object",
                "properties": {
                    "coupon_codes": _str_list,
                    "conditions": {**_str_list, "description": "Coupon terms: minimum spend, exclusions, one-use, online-only..."},
                    "expiry": {**_nullable_str, "description": "When the offer ends."},
                    "deadline": {**_nullable_str, "description": "Date the recipient must act by (pay, reply, renew)."},
                    "event": {**_nullable_str, "description": "Date/time of an appointment, meeting or reservation."},
                    "amount_due": _nullable_str,
                    "reference_numbers": {**_str_list, "description": "Labelled, e.g. 'Account 4821-7733'."},
                    "tracking_numbers": _str_list,
                    "dates": _str_list,
                    "action_items": {**_str_list, "description": "Concrete things the recipient should do, imperative."},
                    "links": _str_list,
                },
                "required": list(k for k in EXTRACT_FIELDS),
            },
        },
        "required": ["category", "summary", "sender_label", "features", "confidence", "importance",
                     "importance_reason", "phishing_risk", "phishing_signs", "task_title", "note_title",
                     "key_link", "key_link_label", "extracted"],
    },
}

PLAN_TOOL = {
    "name": "record_plan",
    "description": "Record the actions the user wants taken on the email.",
    "input_schema": {
        "type": "object",
        "properties": {
            "skip": {"type": "boolean", "description": "True if the user wants to decide later / do nothing now."},
            "understood": {"type": "boolean", "description": "False if the instruction is unclear."},
            "actions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "enum": list(ACTION_TYPES)},
                        "fields": {"type": "array", "items": {"type": "string", "enum": list(EXTRACT_FIELDS)},
                                   "description": "For save_note / add_todo: which extracted fields to include."},
                    },
                    "required": ["type"],
                },
            },
        },
        "required": ["skip", "understood", "actions"],
    },
}

ANALYZE_PROMPT = """You triage email for one person. Analyze the email below.

Categories: {cats}
Features (include every one that applies):
{feats}
Rules of thumb:
- has_coupon only if there is an actual code to type/paste (not just "20% off").
- has_deadline is for things the recipient must do by a date (pay, renew, reply) - NOT an offer's expiry.
- has_amount_due only when the recipient owes money.
- is_bulk for marketing / mailing-list mail.
- Lower confidence when the email is ambiguous.
- Summary and titles must say what the email is REALLY about in plain words (e.g. "class photo order from
  the school" rather than "Shop now"). School newsletters / weekly digests: name them and the school
  (e.g. "Friday Folder - Maple Grove School") and only give a task_title if there is one specific thing to do.
- Links appear as [link N: site]; refer to them by number.
- Phishing: look for a sender address that doesn't match the brand it claims, pressure/urgency, requests for
  passwords or payment details, links pointing to unrelated sites, too-good-to-be-true offers. Be concrete.
  A normal, expected notification from the real company is risk "none".

From: {sender}
Subject: {subject}
Date: {date}
List-Unsubscribe: {unsub}

{body}"""

PLAN_PROMPT = """The user was shown this email and asked what to do with it.

Email ({category}) from {sender}: {summary}
Extracted data available: {available}

Allowed actions: show (display the full email so the user reads it themselves before anything else happens —
use it when they say "make me read it", "I want to see it", "show me"), save_note (save extracted info to notes),
add_todo (create a to-do; fields = details to attach), unsubscribe, archive, delete, keep (leave in inbox).
Keep the user's order; archive/delete/keep go last.
Set skip=true ONLY if the user explicitly says to skip or decide later. Wanting to read the email is NOT skip —
that is the `show` action.
Choose `fields` from the extracted-data keys that the user referred to (e.g. "coupon and its conditions" ->
coupon_codes, conditions, expiry). If they just say "save the important stuff", pick the relevant ones.

User's instruction: "{instruction}\""""


API_URL = "https://api.anthropic.com/v1/messages"


class _Client:
    """Tiny Messages API client on the standard library — no `pip install` needed."""

    def __init__(self):
        self.key = os.environ.get("ANTHROPIC_API_KEY", "").strip()

    def create(self, **payload) -> dict[str, Any]:
        req = urllib.request.Request(
            API_URL, data=json.dumps(payload).encode(), method="POST",
            headers={"x-api-key": self.key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60, context=ssl_context()) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"Claude API error {exc.code}: {detail}") from None


def _client() -> _Client:
    return _Client()


def claude_available() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())


def check_key(key: str, model: str = DEFAULT_MODEL) -> None:
    """Raises if the key doesn't work (used before saving it)."""
    old = os.environ.get("ANTHROPIC_API_KEY")
    os.environ["ANTHROPIC_API_KEY"] = key
    try:
        _Client().create(model=model, max_tokens=5, messages=[{"role": "user", "content": "Say OK"}])
    finally:
        if old is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = old


def _call_tool(client, model: str, prompt: str, tool: dict) -> dict[str, Any]:
    resp = client.create(
        model=model, max_tokens=1500, tools=[tool],
        tool_choice={"type": "tool", "name": tool["name"]},
        messages=[{"role": "user", "content": prompt}],
    )
    for block in resp.get("content", []):
        if block.get("type") == "tool_use":
            return block["input"]
    raise RuntimeError("no tool call in response")


URL_IN_TEXT = re.compile(r"https?://([^/\s<>\")\]]+)[^\s<>\")\]]*")


def prepare_body(text: str, limit: int) -> tuple[str, list[str]]:
    """Numbered, shortened links ("[link 3: ups.com]") so the model reads less but can still point at the
    link that matters; returns the text and the real URLs (index 0 = link 1)."""
    urls: list[str] = []

    def sub(m):
        url = m.group(0).rstrip(".,;")
        if url not in urls:
            urls.append(url)
        return f"[link {urls.index(url) + 1}: {m.group(1).removeprefix('www.')}]"

    text = URL_IN_TEXT.sub(sub, text)
    text = re.sub(r"[ \t\u00a0\u200c\u200b]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    if len(text) > limit:
        text = text[:limit].rsplit(" ", 1)[0] + " …[rest of email cut]"
    return text, urls


_CTA = re.compile(r"^(shop|buy|order|download|click|view|see|learn|read|get|open|sign|join|start|discover|explore|"
                  r"install|watch|book|register|reserve|track)\b[\w\s']{0,25}$", re.I)


def looks_like_cta(text: str) -> bool:
    """'Shop now', 'Download the Team App' — button text, not a useful to-do."""
    return bool(text) and len(text.split()) <= 4 and bool(_CTA.match(text.strip(" .!")))


def analysis_from(data: dict[str, Any], email: Email, engine: str, links: list[str] | None = None) -> Analysis:
    """Structured model output -> Analysis (shared by the Claude and local engines)."""
    feats = [f for f in data.get("features", []) if f in FEATURES]
    if email.header("List-Unsubscribe") and "is_bulk" not in feats:
        feats.append("is_bulk")
    raw = data.get("extracted") or {}
    extracted: dict[str, Any] = {}
    for k in EXTRACT_FIELDS:
        v = raw.get(k)
        if isinstance(v, str) and v.strip().lower() in ("", "none", "null", "n/a"):
            v = None
        if isinstance(v, list):
            v = [str(x).strip() for x in v if str(x).strip()]
        extracted[k] = v
    category = data.get("category") if data.get("category") in CATEGORIES else "other"
    try:
        confidence = max(0.0, min(1.0, float(data.get("confidence", 0.7))))
    except (TypeError, ValueError):
        confidence = 0.7
    # the rule-based checks are a floor: an AI saying "fine" can't hide a concrete warning sign
    base_imp, base_reason, base_risk, base_signs = assess(email, category, feats, extracted)
    ai_risk = data.get("phishing_risk") if data.get("phishing_risk") in RISK_ORDER else "none"
    signs = [str(x) for x in (data.get("phishing_signs") or []) if str(x).strip()]
    signs += [s for s in base_signs if s not in signs]
    risk = max(ai_risk, base_risk, key=RISK_ORDER.get)
    if len(signs) >= 2 and risk != "none":
        risk = "high"
    importance = data.get("importance") if data.get("importance") in ("high", "normal", "low") else base_imp
    if base_imp == "high" or risk == "high":
        importance = "high"
    reason = ("Could be a fake (phishing) email" if risk == "high"
              else (data.get("importance_reason") or "").strip() or base_reason)
    task = str(data.get("task_title") or "").strip()
    if looks_like_cta(task):
        task = ""
    key_link, key_label = "", str(data.get("key_link_label") or "").strip()
    try:
        n = int(data.get("key_link") or 0)
        if links and 1 <= n <= len(links):
            key_link = links[n - 1]
    except (TypeError, ValueError):
        pass
    return Analysis(
        category=category, summary=data.get("summary") or email.subject,
        sender_label=data.get("sender_label") or email.sender_name or email.sender_domain,
        features=feats, extracted=extracted, confidence=confidence, engine=engine,
        importance=importance, importance_reason=reason if importance == "high" else "",
        risk=risk, risk_signs=signs if risk != "none" else [],
        task_title=task, note_title=str(data.get("note_title") or "").strip(),
        key_link=key_link if risk == "none" else "", key_link_label=key_label if key_link else "",
    )


def trust_but_verify(result, text: str, analysis: Analysis, fallback):
    """Models sometimes answer 'skip' or give up on unusual wording. Only accept 'skip' when the
    user actually said so; otherwise let the keyword parser have a go."""
    if result == [] and not SKIP_RE.search(text):
        return fallback.parse(text, analysis)
    if result is None:
        return fallback.parse(text, analysis)
    return result


def actions_from(data: dict[str, Any]) -> list[dict[str, Any]] | None:
    if data.get("skip"):
        return []
    if not data.get("understood", True) or not data.get("actions"):
        return None
    actions = []
    for a in data["actions"]:
        if not isinstance(a, dict) or a.get("type") not in ACTION_TYPES:
            continue
        item: dict[str, Any] = {"type": a["type"]}
        if a["type"] in ("save_note", "add_todo"):
            item["fields"] = [f for f in a.get("fields", []) if f in EXTRACT_FIELDS]
        actions.append(item)
    return actions or None


class ClaudeAnalyzer:
    name = "claude"

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model
        self.client = _client()
        self.fallback = HeuristicAnalyzer()

    def analyze(self, email: Email) -> Analysis:
        body, links = prepare_body(email.body, 8000)
        prompt = ANALYZE_PROMPT.format(
            cats=", ".join(f"{k} ({v})" for k, v in CATEGORIES.items()),
            feats="\n".join(f"- {k}: {v}" for k, v in FEATURES.items()),
            sender=email.sender, subject=email.subject, date=email.date,
            unsub=email.header("List-Unsubscribe") or "(none)", body=body,
        )
        try:
            data = _call_tool(self.client, self.model, prompt, ANALYSIS_TOOL)
        except Exception as exc:  # network, auth, schema...
            print(f"  (Claude unavailable: {exc}; using offline analyzer)", file=sys.stderr)
            return self.fallback.analyze(email)
        return analysis_from(data, email, self.name, links)


class ClaudeInstructionParser:
    name = "claude"

    def __init__(self, model: str = DEFAULT_MODEL):
        self.model = model
        self.client = _client()
        self.fallback = HeuristicInstructionParser()

    def parse(self, text: str, analysis: Analysis) -> list[dict[str, Any]] | None:
        if SKIP_RE.search(text):
            return []
        prompt = PLAN_PROMPT.format(
            category=analysis.category, sender=analysis.sender_label, summary=analysis.summary,
            available=", ".join(analysis.values()) or "(nothing)", instruction=text,
        )
        try:
            data = _call_tool(self.client, self.model, prompt, PLAN_TOOL)
        except Exception as exc:
            print(f"  (Claude unavailable: {exc}; using offline parser)", file=sys.stderr)
            return self.fallback.parse(text, analysis)
        return trust_but_verify(actions_from(data), text, analysis, self.fallback)


def make_engines(engine: str = "auto", model: str = DEFAULT_MODEL, local_model: str | None = None):
    """engine: auto | claude | local | offline"""
    if engine == "local":
        from .local_ai import LocalAnalyzer, LocalInstructionParser, recommended_model
        m = local_model or recommended_model()
        return LocalAnalyzer(m), LocalInstructionParser(m)
    if engine == "claude" or (engine == "auto" and claude_available()):
        if not claude_available():
            raise SystemExit("--engine claude needs ANTHROPIC_API_KEY (or save a key in the app).")
        return ClaudeAnalyzer(model), ClaudeInstructionParser(model)
    return HeuristicAnalyzer(), HeuristicInstructionParser()
