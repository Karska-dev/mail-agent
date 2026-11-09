"""Rule memory and matching.

A rule = conditions (who / what kind / which features) + a workflow (actions).

Matching outcomes
-----------------
auto     an exact rule fits (right sender, category, required features present,
         nothing unexpected) and the analysis is confident -> just do it.
confirm  a rule fits but confidence is low or two rules conflict -> ask yes/no.
suggest  a rule learned for ANOTHER sender fits this email perfectly
         -> offer to reuse it (once / for this sender too / for everyone).
partial  a rule for THIS sender exists but doesn't fully apply
         (e.g. Northwind coupon rule vs. a Northwind ad with no coupon) -> ask.
none     nothing relevant -> ask.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import (FEATURES, FIELD_FEATURE, SIGNIFICANT_FEATURES, Analysis, Email, Rule,
                     describe_actions, describe_features)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class RuleStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.rules: list[Rule] = []
        if self.path.exists():
            self.rules = [Rule.from_dict(d) for d in json.loads(self.path.read_text() or "[]")]

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps([r.to_dict() for r in self.rules], indent=2, ensure_ascii=False))

    def _next_id(self) -> str:
        n = max((int(r.id[1:]) for r in self.rules if r.id[1:].isdigit()), default=0) + 1
        return f"r{n}"

    def add(self, rule: Rule) -> Rule:
        rule.id = self._next_id()
        rule.created_at = rule.created_at or _now()
        self.rules.append(rule)
        self.save()
        return rule

    def get(self, rule_id: str) -> Rule | None:
        return next((r for r in self.rules if r.id == rule_id), None)

    def delete(self, rule_id: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.id != rule_id]
        self.save()
        return len(self.rules) < before

    def record_use(self, rule: Rule) -> None:
        rule.times_applied += 1
        rule.last_applied = _now()
        self.save()


# --------------------------------------------------------------------------- #
# Building rules from a decision
# --------------------------------------------------------------------------- #
SHORT = {"has_coupon": "coupon code", "has_deadline": "deadline", "has_event": "appointment time",
         "has_amount_due": "amount due", "has_reference": "reference no.", "has_tracking": "tracking no.",
         "is_bulk": "bulk mail"}


def rule_name(scope: str, label: str, category: str, required: list[str]) -> str:
    who = label if scope == "sender" else "Any sender"
    what = category
    if required:
        what += " with " + " + ".join(SHORT.get(f, f) for f in required)
    return f"{who} · {what}"


def make_rule(email: Email, analysis: Analysis, actions: list[dict[str, Any]], scope: str,
              instruction: str = "") -> Rule:
    """Learn a rule from what the user decided for this email.

    required  = the significant features the workflow depends on (e.g. saving a
                coupon code requires a coupon).
    excluded  = significant features this email did NOT have; if a future email
                has one, the situation is different enough to ask again.
    """
    feats = set(analysis.features)
    used_fields = {f for a in actions for f in a.get("fields", [])}
    required = sorted({FIELD_FEATURE[f] for f in used_fields if f in FIELD_FEATURE} & feats)
    # a significant feature present in the email is always part of "what this email was"
    required = sorted(set(required) | (feats & set(SIGNIFICANT_FEATURES)))
    excluded = [f for f in SIGNIFICANT_FEATURES if f not in feats]
    return Rule(
        id="", name=rule_name(scope, analysis.sender_label, analysis.category, required),
        scope=scope,
        sender_domain=email.sender_domain if scope == "sender" else None,
        sender_label=analysis.sender_label if scope == "sender" else "Any sender",
        category=analysis.category,
        required_features=required,
        excluded_features=excluded,
        actions=[dict(a) for a in actions],
        instruction=instruction,
    )


def copy_rule(rule: Rule, *, scope: str, email: Email | None = None, analysis: Analysis | None = None) -> Rule:
    label = analysis.sender_label if (scope == "sender" and analysis) else "Any sender"
    return Rule(
        id="", name=rule_name(scope, label, rule.category, rule.required_features),
        scope=scope,
        sender_domain=email.sender_domain if (scope == "sender" and email) else None,
        sender_label=label, category=rule.category,
        required_features=list(rule.required_features), excluded_features=list(rule.excluded_features),
        actions=[dict(a) for a in rule.actions], instruction=rule.instruction, derived_from=rule.id,
    )


def explain_rule(rule: Rule) -> str:
    who = f"from {rule.sender_label} ({rule.sender_domain})" if rule.scope == "sender" else "from any sender"
    lines = [f"[{rule.id}] {rule.name}",
             f"    when: {rule.category} emails {who}"]
    if rule.required_features:
        lines.append(f"          that contain {describe_features(rule.required_features)}")
    if rule.excluded_features:
        lines.append("    asks instead if it has a: " + ", ".join(SHORT.get(f, f) for f in rule.excluded_features))
    lines.append(f"    do:   {describe_actions(rule.actions)}")
    if rule.instruction:
        lines.append(f"    you said: \"{rule.instruction}\"")
    lines.append(f"    used {rule.times_applied}×" + (f", derived from {rule.derived_from}" if rule.derived_from else ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Matching
# --------------------------------------------------------------------------- #
@dataclass
class Match:
    kind: str  # auto | confirm | suggest | partial | none
    rule: Rule | None = None
    reason: str = ""
    others: list[Rule] = field(default_factory=list)


def _specificity(r: Rule) -> tuple:
    return (r.scope == "sender", len(r.required_features), r.times_applied, r.created_at)


def evaluate(email: Email, analysis: Analysis, rules: list[Rule], min_confidence: float = 0.6) -> Match:
    feats = set(analysis.features)
    exact: list[Rule] = []
    similar: list[Rule] = []
    partial: list[tuple[Rule, list[str], list[str]]] = []

    for r in rules:
        same_sender = r.scope == "sender" and r.sender_domain == email.sender_domain
        sender_ok = r.scope == "any" or same_sender
        cat_ok = r.category == analysis.category
        missing = [f for f in r.required_features if f not in feats]
        unexpected = [f for f in r.excluded_features if f in feats]
        fits = cat_ok and not missing and not unexpected
        if sender_ok and fits:
            exact.append(r)
        elif fits and r.scope == "sender":
            similar.append(r)
        elif same_sender and cat_ok:
            partial.append((r, missing, unexpected))

    if exact:
        exact.sort(key=_specificity, reverse=True)
        best = exact[0]
        rivals = [r for r in exact[1:] if _specificity(r)[:2] == _specificity(best)[:2]
                  and r.actions != best.actions]
        if analysis.confidence < min_confidence:
            return Match("confirm", best, f"I'm only {int(analysis.confidence * 100)}% sure I read this email right.")
        if rivals:
            return Match("confirm", best, "More than one of your rules fits this email, with different actions.",
                         others=rivals)
        return Match("auto", best, "exact match")

    if similar:
        similar.sort(key=lambda r: (r.times_applied, r.created_at), reverse=True)
        best = similar[0]
        return Match("suggest", best,
                     f"This looks like the {best.sender_label} workflow, but it's from {analysis.sender_label}.",
                     others=similar[1:])

    if partial:
        r, missing, unexpected = partial[0]
        bits = [f"this email has no {SHORT.get(f, f)}" for f in missing]
        bits += [f"it also contains {FEATURES[f]}, which that rule wasn't set up for" for f in unexpected]
        reason = (f"Your rule \"{r.name}\" ({describe_actions(r.actions)}) doesn't fully apply: "
                  + "; ".join(bits) + ".")
        return Match("partial", r, reason)

    return Match("none")
