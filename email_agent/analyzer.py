"""Offline email analyzer.

Regex + keyword heuristics that produce the same structured Analysis the
Claude analyzer does. Used when no API key is configured, and as a fallback
if an API call fails.
"""
from __future__ import annotations

import re

from .models import Analysis, Email

MONTHS = (r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|"
          r"Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)")
DATE = (rf"(?:{MONTHS}\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:,?\s+\d{{4}})?"
        rf"|\d{{1,2}}\s+{MONTHS}(?:,?\s+\d{{4}})?"
        r"|\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?)")
TIME = r"\d{1,2}(?::\d{2})?\s?(?:[AaPp]\.?[Mm]\.?)"

DATE_RE = re.compile(rf"\b{DATE}\b", re.I)
COUPON_RE = re.compile(r"(?i:promo\s*code|coupon\s*code|discount\s*code|voucher|coupon|code)\s*[:\-]?\s*[\"“']?"
                       r"([A-Z0-9][A-Z0-9\-]{3,19})\b")
AMOUNT_RE = re.compile(r"[$€£¥]\s?\d[\d,]*(?:\.\d{2})?")
DEADLINE_RE = re.compile(rf"(?:due(?:\s+date)?(?:\s+(?:on|by))?|pay(?:ment)?\s+(?:is\s+)?due(?:\s+(?:on|by))?|pay\s+by|"
                         rf"respond\s+by|reply\s+by|deadline|no later than|must be \w+ by|renew\s+by)\s*[:\-]?\s*({DATE})", re.I)
EXPIRY_RE = re.compile(rf"(?:valid\s+(?:until|through|thru|till)|expires?(?:\s+on)?|ends?(?:\s+on)?|until|through)\s*[:\-]?\s*({DATE})", re.I)
EVENT_RE = re.compile(rf"({DATE}(?:,?\s+(?:at\s+)?{TIME})?)", re.I)
REF_RE = re.compile(r"\b(order|invoice|account|reference|ref|confirmation|booking|case|ticket|policy)\s*"
                    r"(?:no\.?|number|#|id)?\s*[:#]?\s*([A-Z0-9][A-Z0-9\-]{4,})", re.I)
TRACKING_RE = re.compile(r"tracking\s*(?:number|no\.?|#)?\s*[:#]?\s*([A-Z0-9]{8,})", re.I)
URL_RE = re.compile(r"https?://[^\s<>\")]+")
CODE_STOPWORDS = {"CODE", "COUPON", "CHECKOUT", "ONLINE", "PROMO", "BELOW", "TODAY", "ORDER", "BELOW:"}

CONDITION_WORDS = re.compile(r"\b(valid|expire|minimum|min\.|orders? over|excludes?|exclusions?|only|one use|per customer|"
                             r"cannot be combined|not valid|applies|in-store|online|restrictions?|terms)\b", re.I)
ACTION_WORDS = re.compile(r"\b(please|pay|confirm|reply|respond|schedule|reschedule|complete|submit|renew|call|sign|"
                          r"review|verify|update|register|rsvp)\b", re.I)

CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "bill": ["amount due", "payment due", "your bill", "invoice", "statement is ready", "balance due",
             "total due", "autopay", "pay by", "minimum payment"],
    "receipt": ["receipt", "order confirmation", "thank you for your order", "you paid", "payment received",
                "order has been placed"],
    "shipping": ["shipped", "tracking number", "out for delivery", "has been delivered", "on its way", "shipment"],
    "appointment": ["appointment", "reservation", "booking confirmed", "scheduled for", "check-in", "your visit",
                    "see you on"],
    "security": ["password", "verification code", "sign-in", "sign in to", "signed in", "new login", "security alert",
                 "two-factor", "2fa", "apple id was used", "new device", "unusual activity", "suspended",
                 "verify your account", "login attempt"],
    "newsletter": ["newsletter", "digest", "this week in", "issue #", "weekly roundup", "read more", "top stories"],
    "promotion": ["% off", "sale", "discount", "promo", "coupon", "shop now", "new arrivals", "limited time",
                  "deal", "free shipping", "collection", "exclusive", "members-only", "discover"],
    "social": ["commented on", "liked your", "mentioned you", "friend request", "new follower", "tagged you"],
}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<!\bDr\.)(?<!\bMr\.)(?<!\bMs\.)(?<!\bMrs\.)(?<!\bSt\.)(?<=[.!?])\s+|\n+", text) if s.strip()]


def _uniq(items):
    seen, out = set(), []
    for i in items:
        if i not in seen:
            seen.add(i)
            out.append(i)
    return out


class HeuristicAnalyzer:
    name = "offline"

    def analyze(self, email: Email) -> Analysis:
        text = f"{email.subject}\n{email.body}"
        low = text.lower()
        sentences = _sentences(email.body)
        bulk = bool(email.header("List-Unsubscribe")) or "unsubscribe" in low

        # --- extraction --------------------------------------------------
        codes = _uniq(c for c in COUPON_RE.findall(text)
                      if c not in CODE_STOPWORDS and (re.search(r"\d", c) or len(c) >= 6))
        conditions = []
        if codes:
            conditions = _uniq([s for s in sentences if len(s.split()) >= 4 and "unsubscribe" not in s.lower()
                                and (CONDITION_WORDS.search(s) or any(c in s for c in codes))])[:4]
        expiry = (m := EXPIRY_RE.search(text)) and m.group(1)
        deadline = (m := DEADLINE_RE.search(text)) and m.group(1)
        amounts = _uniq(AMOUNT_RE.findall(text))
        amount_due = None
        for s in sentences:
            if re.search(r"\b(due|balance|total|pay)\b", s, re.I) and (a := AMOUNT_RE.search(s)):
                amount_due = a.group(0)
                break
        refs = _uniq(f"{k.title()} {v}" for k, v in REF_RE.findall(text) if re.search(r"\d", v))
        tracking = _uniq(TRACKING_RE.findall(text))
        dates = _uniq(DATE_RE.findall(text))
        links = _uniq(URL_RE.findall(email.body))[:5]
        action_items = [s for s in sentences if ACTION_WORDS.search(s) and "unsubscribe" not in s.lower()][:3]

        # --- category ----------------------------------------------------
        scores = {cat: sum(low.count(k) for k in kws) for cat, kws in CATEGORY_KEYWORDS.items()}
        if codes:
            scores["promotion"] += 3
        if amount_due and deadline:
            scores["bill"] += 3
        if tracking:
            scores["shipping"] += 3
        ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        (best, top), (_, second) = ranked[0], ranked[1]
        if top == 0:
            category = "other" if bulk else "personal"
            confidence = 0.55
        else:
            category = best
            confidence = min(0.95, 0.6 + 0.1 * (top - second))

        event = None
        if category == "appointment" or re.search(r"\b(appointment|meeting|reservation|booking)\b", low):
            m = EVENT_RE.search(email.body)
            event = m.group(1).rstrip(".") if m else None

        features = []
        if codes:
            features.append("has_coupon")
        if deadline:
            features.append("has_deadline")
        if event:
            features.append("has_event")
        if amount_due and category in ("bill", "receipt", "other", "personal", "work"):
            features.append("has_amount_due")
        if refs:
            features.append("has_reference")
        if tracking:
            features.append("has_tracking")
        if bulk:
            features.append("is_bulk")

        first = next((s for s in sentences if len(s.split()) >= 5), email.subject)
        summary = first if len(first) < 160 else first[:157] + "..."
        label = email.sender_name or email.sender_domain.split(".")[0].title()
        if category == "bill" and (amount_due or deadline):
            action_items.insert(0, f"Pay {label} bill" + (f" ({amount_due})" if amount_due else ""))
        elif category == "appointment" and event:
            action_items.insert(0, f"{label} appointment — {event.rstrip('.')}")

        importance, reason, risk, signs = assess(email, category, features, {"deadline": deadline, "event": event})
        task = action_items[0] if category in ("bill", "appointment") and action_items else ""
        key_link, key_label = ("", "") if risk != "none" else _key_link(email.body)
        return Analysis(
            task_title=task, note_title=f"{label} — {email.subject}"[:80],
            key_link=key_link, key_link_label=key_label,
            importance=importance, importance_reason=reason, risk=risk, risk_signs=signs,
            category=category,
            summary=summary,
            sender_label=label,
            features=features,
            extracted={
                "coupon_codes": codes,
                "conditions": conditions,
                "expiry": expiry or None,
                "deadline": deadline or None,
                "event": event,
                "amount_due": amount_due,
                "reference_numbers": refs,
                "tracking_numbers": tracking,
                "dates": dates,
                "action_items": action_items,
                "links": links,
                "amounts": amounts,
            },
            confidence=round(confidence, 2),
            engine=self.name,
        )


# --------------------------------------------------------------------------- #
# Importance and "is this a fake?" checks (used by every reader as a baseline)
# --------------------------------------------------------------------------- #
BRANDS = ["paypal", "apple", "icloud", "google", "gmail", "microsoft", "outlook", "amazon", "netflix", "chase",
          "wells fargo", "bank of america", "citi", "capital one", "amex", "american express", "irs", "dhl",
          "fedex", "ups", "usps", "instagram", "facebook", "meta", "whatsapp", "linkedin", "coinbase", "venmo",
          "zelle", "docusign", "dropbox", "adobe", "verizon", "at&t", "t-mobile", "comcast", "xfinity"]
URGENT_RE = re.compile(r"\b(verify your (?:account|identity)|account (?:has been |will be )?(?:suspended|locked|closed|"
                       r"disabled|limited)|unusual activity|confirm your (?:password|details|payment)|within 24 hours|"
                       r"immediately|urgent(?:ly)?|final (?:notice|warning)|click (?:here|below) to|update your "
                       r"(?:payment|billing)|failed (?:payment|delivery)|you(?:'ve| have) won|gift ?cards?|wire transfer"
                       r"|reset your password now)\b", re.I)
CREDENTIALS_RE = re.compile(r"\b(enter|provide|send|reply with|confirm) (?:your )?(?:password|passcode|pin|social "
                            r"security|ssn|card number|cvv|bank (?:details|account)|login)\b", re.I)
SHORTENERS = {"bit.ly", "tinyurl.com", "t.co", "goo.gl", "ow.ly", "is.gd", "rb.gy", "cutt.ly"}
RISK_ORDER = {"none": 0, "low": 1, "high": 2}


def _link_domains(body: str) -> list[str]:
    from .models import org_domain
    out = []
    for url in URL_RE.findall(body):
        host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0].lower()
        if host:
            out.append(host if host in SHORTENERS else org_domain(host))
    return _uniq(out)


def phishing_signs(email: Email, category: str) -> list[str]:
    signs: list[str] = []
    text = f"{email.subject}\n{email.body}"
    low = text.lower()
    dom = email.sender_domain
    name = (email.sender_name or "").lower()
    first_label = dom.split(".")[0]
    for brand in BRANDS:
        token = brand.replace(" ", "").replace("&", "")
        claims = brand in name or re.search(rf"\b{re.escape(brand)}\b", email.subject.lower())
        if token in first_label.replace("-", "") and first_label != token:
            signs.append(f"Lookalike address: {dom} is not {brand.title()}'s real domain")
            break
        if claims and token not in first_label.replace("-", ""):
            signs.append(f"Says it's from {brand.title()}, but it was sent from {dom}")
            break
    if (m := URGENT_RE.search(text)):
        signs.append(f"Pressure / urgent wording (“{m.group(0)}”)")
    if CREDENTIALS_RE.search(text):
        signs.append("Asks for a password, PIN or payment details — real companies don't do this by email")
    reply_to = email.header("Reply-To") or ""
    if "@" in reply_to:
        from .models import org_domain
        rdom = org_domain(reply_to.split("@")[-1].strip(" >"))
        if rdom and rdom != dom:
            signs.append(f"Replies would go to a different address ({rdom})")
    links = _link_domains(email.body)
    if any(d in SHORTENERS for d in links):
        signs.append("Uses shortened links that hide where they really go")
    elif (category == "security" or signs) and links:
        others = [d for d in links if d != dom]
        if others:
            signs.append(f"Links go to {', '.join(others[:2])}, not {dom}")
    return signs


def assess(email: Email, category: str, features: list[str], extracted: dict) -> tuple[str, str, str, list[str]]:
    """-> (importance, reason, risk, risk_signs)"""
    signs = phishing_signs(email, category)
    risk = "high" if len(signs) >= 2 else "low" if signs else "none"
    if risk == "high":
        return "high", "Could be a fake (phishing) email", risk, signs
    if category == "security":
        return "high", "Security alert about one of your accounts", risk, signs
    if "has_amount_due" in features or "has_deadline" in features:
        due = extracted.get("deadline")
        return "high", "Needs action" + (f" by {due}" if due else ""), risk, signs
    if "has_event" in features:
        return "high", f"Appointment {extracted.get('event') or ''}".strip(), risk, signs
    if category in ("personal", "work"):
        return "high", "From a real person", risk, signs
    if category in ("promotion", "newsletter", "social"):
        return "low", "", risk, signs
    return "normal", "", risk, signs


KEY_LINK_WORDS = re.compile(r"\b(track|tracking|order|pay|payment|bill|view (?:your )?(?:order|statement|bill)|"
                            r"confirm|rsvp|register|sign up|form|schedule|reschedule|appointment|download)\b", re.I)


def _key_link(body: str) -> tuple[str, str]:
    """HTML mail becomes "anchor text (url)"; pick the first link whose text looks like the thing to do."""
    for m in re.finditer(r"([A-Za-z][^()\n]{2,50}?)\s*\((https?://[^)\s]+)\)", body):
        label = m.group(1).strip(" :-|•")
        if KEY_LINK_WORDS.search(label) and "unsubscribe" not in label.lower():
            return m.group(2), label[:40]
    return "", ""
