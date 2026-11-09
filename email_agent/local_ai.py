"""Local AI via Ollama (https://ollama.com): free, and emails never leave your Mac.

Ollama runs open models (Qwen, Llama, Gemma…) on your own computer and listens on
http://127.0.0.1:11434. We ask it for JSON that matches a schema, the same structure the
Claude engine produces, so the rest of the agent doesn't care which one is used.

Small local models occasionally miss things, so the built-in reader acts as a safety net:
anything it finds with high precision (coupon codes, account/order numbers, tracking numbers)
is merged in if the model left it out.
"""
from __future__ import annotations

import json
import os
import re
import threading
import subprocess
import sys
import urllib.error
import urllib.request
from functools import lru_cache
from typing import Any, Callable

from .analyzer import HeuristicAnalyzer
from .instructions import SKIP_RE, HeuristicInstructionParser
from .llm import (ANALYSIS_TOOL, ANALYZE_PROMPT, PLAN_PROMPT, PLAN_TOOL, actions_from, analysis_from,
                  prepare_body, trust_but_verify)
from .models import CATEGORIES, FEATURES, Analysis, Email

OLLAMA_URL = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
if not OLLAMA_URL.startswith("http"):
    OLLAMA_URL = "http://" + OLLAMA_URL

# (model tag, download size, who it's for)
# (model tag, download size, who it's for, is_new). Checked against ollama.com/library in Sep 2026;
# the newer families are marked so the app can say "new" — try them in practice mode first.
RECOMMENDED = [
    ("qwen3.5:9b", "6.6 GB", "newest Qwen, best quality here — Macs with 16 GB of memory or more", True),
    ("qwen3.5:4b", "3.4 GB", "newest Qwen, small and fast — good for 8 GB Macs", True),
    ("gemma4:e4b", "≈7 GB", "Google's newest small model, strong at structured answers — 16 GB Macs", True),
    ("qwen2.5:7b", "4.7 GB", "older but proven with this app — 16 GB Macs", False),
    ("qwen2.5:3b", "1.9 GB", "older, fastest — 8 GB Macs", False),
]
# model families that "think out loud" before answering; for email triage that only costs time
THINKING_FAMILIES = re.compile(r"^(qwen3|gemma4|granite4|deepseek-r1|gpt-oss|magistral|phi4-.*reason)", re.I)
URL_IN_TEXT = re.compile(r"https?://([^/\s<>\")]+)[^\s<>\")]*")
BODY_LIMIT = 3500          # characters of the email the model reads (the start is where the content is)

JSON_ONLY = ("Reply with a single JSON object that matches the requested structure. "
             "Use an empty string or empty list when something is not present. No other text.")


# --------------------------------------------------------------------------- #
def _request(path: str, payload: dict | None = None, timeout: float = 180):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(OLLAMA_URL + path, data=data, method="POST" if data else "GET",
                                 headers={"content-type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def status() -> dict[str, Any]:
    """Is Ollama running, and which models are downloaded?"""
    try:
        with _request("/api/tags", timeout=1.5) as r:
            models = [m["name"] for m in json.loads(r.read()).get("models", [])]
        return {"running": True, "models": models}
    except Exception:
        return {"running": False, "models": []}


@lru_cache(maxsize=1)
def mac_memory_gb() -> int | None:
    if sys.platform != "darwin":
        return None
    try:
        out = subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout
        return round(int(out.strip()) / 1024 ** 3)
    except Exception:
        return None


def recommended_model() -> str:
    mem = mac_memory_gb()
    return "qwen3.5:4b" if mem is not None and mem < 16 else "qwen3.5:9b"


def compact_body(text: str, limit: int = BODY_LIMIT) -> str:
    """Shrink what the model has to read: long links become [link N: site], blank space collapses."""
    return prepare_body(text, limit)[0]


def warm_up(model: str) -> None:
    """Load the model into memory in the background, so the first email doesn't pay for it."""
    def go():
        try:
            with _request("/api/generate", {"model": model, "prompt": "", "keep_alive": "15m"}, timeout=300):
                pass
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


def pull(model: str, progress: Callable[[dict[str, Any]], None]) -> None:
    """Download a model, reporting {"status", "completed", "total"} as it goes."""
    with _request("/api/pull", {"model": model, "stream": True}, timeout=3600) as r:
        for line in r:
            if line.strip():
                msg = json.loads(line)
                if msg.get("error"):
                    raise RuntimeError(msg["error"])
                progress(msg)


def _schema_for_local(schema: dict) -> dict:
    """Ollama's structured output wants plain types: turn ["string","null"] into "string"."""
    if isinstance(schema, dict):
        out = {}
        for k, v in schema.items():
            if k == "type" and isinstance(v, list):
                out[k] = next((t for t in v if t != "null"), "string")
            elif k == "description":
                continue
            else:
                out[k] = _schema_for_local(v)
        return out
    if isinstance(schema, list):
        return [_schema_for_local(x) for x in schema]
    return schema


def _slim(schema: dict) -> dict:
    """The local model skips fields the built-in reader finds reliably (links, dates): less to write = faster."""
    ex = schema["properties"]["extracted"]
    for k in ("links", "dates"):
        ex["properties"].pop(k, None)
        if k in ex.get("required", []):
            ex["required"].remove(k)
    return schema


ANALYSIS_SCHEMA = _slim(_schema_for_local(ANALYSIS_TOOL["input_schema"]))
BREVITY = ("\n\nKeep it short: summary max 20 words; task_title and note_title max 10 words each; at most 3 conditions and 2 action_items, each under 15 words; "
           "phishing_signs only if there really are some.")
PLAN_SCHEMA = _schema_for_local(PLAN_TOOL["input_schema"])


def _chat(model: str, prompt: str, schema: dict) -> dict[str, Any]:
    payload = {
        "model": model, "stream": False, "format": schema,
        "messages": [{"role": "system", "content": JSON_ONLY}, {"role": "user", "content": prompt}],
        # smaller context = less memory and faster; num_predict caps the answer length
        "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 500},
        "keep_alive": "15m",
    }
    if THINKING_FAMILIES.match(model):
        payload["think"] = False   # skip the long hidden reasoning: much faster, same answers for this job
    try:
        with _request("/api/chat", payload) as r:
            body = json.loads(r.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        if "think" in detail.lower() and "think" in payload:   # this model/Ollama doesn't take the switch
            payload.pop("think")
            with _request("/api/chat", payload) as r:
                body = json.loads(r.read())
        else:
            raise RuntimeError(f"Ollama error {exc.code}: {detail}") from None
    return json.loads(body["message"]["content"])


# --------------------------------------------------------------------------- #
_SAFETY_LISTS = {"coupon_codes": "has_coupon", "reference_numbers": "has_reference",
                 "tracking_numbers": "has_tracking"}
_SAFETY_VALUES = {"deadline": "has_deadline", "amount_due": "has_amount_due", "event": "has_event",
                  "expiry": None}


def merge_safety_net(a: Analysis, h: Analysis) -> Analysis:
    """Fill gaps the local model left, using the built-in reader's high-precision finds."""
    for key, feat in _SAFETY_LISTS.items():
        if h.extracted.get(key) and not a.extracted.get(key):
            a.extracted[key] = h.extracted[key]
            if feat and feat not in a.features:
                a.features.append(feat)
    for key in ("links", "dates"):   # not asked of the local model — always from the built-in reader
        if not a.extracted.get(key):
            a.extracted[key] = h.extracted.get(key) or []
    for key, feat in _SAFETY_VALUES.items():
        if h.extracted.get(key) and not a.extracted.get(key):
            a.extracted[key] = h.extracted[key]
            if feat and feat not in a.features and feat in h.features:
                a.features.append(feat)
    if a.extracted.get("coupon_codes") and not a.extracted.get("conditions"):
        a.extracted["conditions"] = h.extracted.get("conditions") or []
    if not a.key_link and h.key_link and a.risk == "none":
        a.key_link, a.key_link_label = h.key_link, h.key_link_label
    a.note_title = a.note_title or h.note_title
    # features the model claimed but has nothing to back them up
    if "has_coupon" in a.features and not a.extracted.get("coupon_codes"):
        a.features.remove("has_coupon")
    return a


class LocalAnalyzer:
    name = "local"

    def __init__(self, model: str):
        self.model = model
        self.fallback = HeuristicAnalyzer()

    def analyze(self, email: Email) -> Analysis:
        heuristic = self.fallback.analyze(email)
        body, links = prepare_body(email.body, BODY_LIMIT)
        prompt = ANALYZE_PROMPT.format(
            cats=", ".join(f"{k} ({v})" for k, v in CATEGORIES.items()),
            feats="\n".join(f"- {k}: {v}" for k, v in FEATURES.items()),
            sender=email.sender, subject=email.subject, date=email.date,
            unsub=email.header("List-Unsubscribe") or "(none)", body=body,
        ) + BREVITY
        try:
            data = _chat(self.model, prompt, ANALYSIS_SCHEMA)
        except Exception as exc:
            print(f"  (local AI unavailable: {exc}; using built-in reader)", file=sys.stderr)
            return heuristic
        return merge_safety_net(analysis_from(data, email, f"local · {self.model}", links), heuristic)


class LocalInstructionParser:
    name = "local"

    def __init__(self, model: str):
        self.model = model
        self.fallback = HeuristicInstructionParser()

    def parse(self, text: str, analysis: Analysis) -> list[dict[str, Any]] | None:
        if SKIP_RE.search(text):
            return []
        # short, plain answers ("Archive it", "Delete it", the quick-answer buttons) don't need the AI
        quick = self.fallback.parse(text, analysis)
        if quick and len(text.split()) <= 8:
            return quick
        prompt = PLAN_PROMPT.format(
            category=analysis.category, sender=analysis.sender_label, summary=analysis.summary,
            available=", ".join(analysis.values()) or "(nothing)", instruction=text,
        )
        try:
            result = actions_from(_chat(self.model, prompt, PLAN_SCHEMA))
        except Exception as exc:
            print(f"  (local AI unavailable: {exc}; using built-in parser)", file=sys.stderr)
            return self.fallback.parse(text, analysis)
        return trust_but_verify(result, text, analysis, self.fallback)
