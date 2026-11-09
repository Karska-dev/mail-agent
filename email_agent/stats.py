"""Insights: how well the agent is doing, worked out from the activity log and events.

Everything is read from files in the data folder and stays on this computer.
Practice runs never count (nothing was changed), and neither do failed moves.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Any

from .agent import outcome_of, rule_id_of

SECONDS_SAVED_PER_AUTO_EMAIL = 30   # assumption behind "time saved"; shown to the user as such
HANDLED = ("auto", "asked", "one_off")


def _load(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text() or "[]") if path.exists() else []
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _when(item: dict[str, Any]) -> datetime | None:
    try:
        return datetime.fromisoformat(item.get("at", ""))
    except ValueError:
        return None


def _within(items, start: datetime, end: datetime):
    return [i for i in items if (t := _when(i)) is not None and start <= t < end]


def _sender_name(sender: str) -> str:
    m = re.match(r'\s*"?([^"<]+?)"?\s*<', sender or "")
    return (m.group(1) if m else (sender or "").strip("<> ")).strip() or "Unknown sender"


def _outcome(entry: dict[str, Any]) -> str:
    return entry.get("outcome") or outcome_of(entry.get("source", ""), False)


def _core(log: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
    handled = [e for e in log if _outcome(e) in HANDLED]
    auto = [e for e in handled if _outcome(e) == "auto"]
    asked = [e for e in handled if _outcome(e) != "auto"]
    total = len(handled)
    restores = [e for e in events if e.get("type") == "restore"]
    restores_after_auto = [e for e in restores if e.get("outcome") == "auto"]
    return {"handled": handled, "auto": auto, "asked": asked, "total": total,
            "restores": restores, "restores_after_auto": restores_after_auto}


def compute(data_dir: Path, days: int = 30, now: datetime | None = None) -> dict[str, Any]:
    """The Insights numbers for the last `days` days, plus the question rate of the period before."""
    data_dir = Path(data_dir)
    now = now or datetime.now()
    start, prev_start = now - timedelta(days=days), now - timedelta(days=2 * days)
    log = _load(data_dir / "activity_log.json")
    events = _load(data_dir / "events.json")
    todos = _load(data_dir / "todos.json")
    rules = {r.get("id"): r for r in _load(data_dir / "rules.json")}

    cur = _core(_within(log, start, now + timedelta(seconds=1)), _within(events, start, now + timedelta(seconds=1)))
    prev = _core(_within(log, prev_start, start), _within(events, prev_start, start))
    total, n_auto = cur["total"], len(cur["auto"])

    def per100(c) -> float | None:
        return round(100 * len(c["asked"]) / c["total"], 1) if c["total"] else None

    # most-used rules (among emails handled automatically in the period)
    used = Counter(e.get("rule_id") or rule_id_of(e.get("source", "")) for e in cur["auto"])
    deleted = {e.get("rule_id"): e.get("name") for e in events if e.get("type") == "rule_deleted"}
    top_rules = []
    for rid, n in used.most_common(3):
        r = rules.get(rid)
        top_rules.append({"rule_id": rid, "name": (r or {}).get("name") or deleted.get(rid) or rid, "count": n,
                          "exists": r is not None})

    # senders still asked about, more than once
    asked_senders = Counter(_sender_name(e.get("from", "")) for e in cur["asked"])
    ask_again = [{"sender": s, "count": n} for s, n in asked_senders.most_common() if n >= 2][:3]

    actions = Counter()
    for e in cur["handled"]:
        for a in set(e.get("actions") or []):
            actions[a] += 1
    made = [t for t in todos if (c := _when({"at": t.get("created", "")})) and start <= c <= now]
    done = [t for t in made if t.get("done")]
    secs = [e["seconds"] for e in cur["handled"] if isinstance(e.get("seconds"), (int, float)) and e["seconds"] > 0]

    put_back_rate = (100 * len(cur["restores_after_auto"]) / n_auto) if n_auto else 0.0
    return {
        "days": days,
        "emails": total,
        "auto": n_auto,
        "auto_pct": round(100 * n_auto / total) if total else 0,
        "asked": len(cur["asked"]),
        "questions_per_100": per100(cur),
        "questions_per_100_before": per100(prev),
        "put_back_total": len(cur["restores"]),
        "put_back_after_auto": len(cur["restores_after_auto"]),
        "kept_pct": round(100 - put_back_rate, 1) if n_auto else None,
        "trust_hearts": max(0, 4 - round(put_back_rate)) if n_auto else 4,
        "top_rules": top_rules,
        "ask_again": ask_again,
        "archived": actions["archive"], "deleted": actions["delete"], "unsubscribed": actions["unsubscribe"],
        "todos_made": len(made), "todos_done": len(done),
        "median_read_seconds": round(median(secs)) if secs else None,
        "minutes_saved": round(n_auto * SECONDS_SAVED_PER_AUTO_EMAIL / 60),
        "seconds_per_email_assumed": SECONDS_SAVED_PER_AUTO_EMAIL,
        "rules_deleted": sum(1 for e in _within(events, start, now + timedelta(seconds=1)) if e.get("type") == "rule_deleted"),
    }
