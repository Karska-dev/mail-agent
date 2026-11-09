"""Clear out old notes and to-dos.

"Old" means the *email* is old (its Date header), not when the note was made: a big inbox clean-up
creates hundreds of notes in one evening about mail from years ago. Items without a saved original
fall back to when they were created.

Nothing is thrown away for good: removed items go to cleared/<timestamp>.json, and the last clean-up
can be undone.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MON = (r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
        r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\.?")
_MONTH_DAY = re.compile(r"\b" + _MON + r"\s+(\d{1,2})(?:st|nd|rd|th)?\b(?:,?\s+(\d{4}))?", re.I)
_DAY_MONTH = re.compile(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+" + _MON + r"(?![a-z])(?:,?\s+(\d{4}))?", re.I)
_FILLER = re.compile(r"\b(due|by|on|before|until|till|at|the|of|mon|tue|wed|thu|fri|sat|sun)[a-z]*\b|"
                     r"\d{1,2}:\d{2}(:\d{2})?\s*(am|pm)?", re.I)
_ISO = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_US = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b")


def parse_due(text: str | None, today: date | None = None) -> date | None:
    """Best effort: 'October 15, 2026', 'Oct 3', '3 Oct', '2026-10-15', '10/15/2026'. None if unsure.
    A date without a year is taken as the next one after `today` (pass the email's date here)."""
    if not text:
        return None
    today = today or date.today()
    try:
        if m := _ISO.search(text):
            return date(int(m[1]), int(m[2]), int(m[3]))
        if m := _US.search(text):
            y = int(m[3]) + (2000 if len(m[3]) == 2 else 0)
            return date(y, int(m[1]), int(m[2]))
        for rx, mon_i, day_i in ((_MONTH_DAY, 1, 2), (_DAY_MONTH, 2, 1)):
            m = next((m for m in rx.finditer(text) if m[mon_i] != "may"), None)   # "you may 5…" is not May 5
            if m:
                mon, day = MONTHS[m[mon_i][:3].lower()], int(m[day_i])
                if m[3]:
                    return date(int(m[3]), mon, day)
                d = date(today.year, mon, day)   # no year: the next time that date comes round
                return d if d >= today - timedelta(days=31) else date(today.year + 1, mon, day)
    except ValueError:
        return None
    return None


def todo_when(t: dict[str, Any], sent: date | None, today: date | None = None) -> dict[str, Any] | None:
    """The date a to-do is about, for showing it on its own line: from its due date, or failing that
    a date in its title / summary / event. days = how far away (negative = past)."""
    today = today or date.today()
    ref = sent or today
    due = (t.get("due") or "").strip()
    d = parse_due(due, ref) if due else None
    if d is None and not due:
        details = t.get("details") or {}
        for text in (t.get("title"), details.get("deadline"), details.get("event"), t.get("about")):
            if isinstance(text, str) and (d := parse_due(text, ref)):
                break
    if d is None:
        return {"date": None, "text": due} if due else None
    # keep the original wording when it says more than the date ("…meeting on 24 Sep at 7:00pm")
    rest = due
    for rx in (_ISO, _US, _MONTH_DAY, _DAY_MONTH, _FILLER):
        rest = rx.sub(" ", rest)
    extra = due if len(re.sub(r"[\W\d_]", "", rest)) >= 5 else ""
    tm = re.search(r"\b(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)(?![a-z])", due, re.I)
    return {"date": d.isoformat(), "days": (d - today).days, "text": extra,
            "time": re.sub(r"\s+|\.", "", tm[1]).lower() if tm else ""}


def _parse_sent(text: str) -> datetime | None:
    """An email's Date header ('Fri, 6 Nov 2026 10:00:00 -0500'), or a plain '2026-11-06'."""
    try:
        return parsedate_to_datetime(text)
    except Exception:
        pass
    try:
        d = datetime.fromisoformat(text.strip())
        return d if d.tzinfo else d.replace(hour=12).astimezone()
    except ValueError:
        return None


def _email_dates(originals) -> dict[int, datetime]:
    out = {}
    for e in originals.index:
        d = _parse_sent(e.get("date") or "")
        if d is not None:
            out[e["ref"]] = d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    return out


def _created(item: dict[str, Any]) -> datetime | None:
    try:
        d = datetime.fromisoformat(item.get("created") or "")
        return d if d.tzinfo else d.astimezone()
    except ValueError:
        return None


def plan(todos, notes, originals, days: int = 30, keep_upcoming: bool = True,
         now: datetime | None = None) -> dict[str, Any]:
    """Which items a clean-up would remove (ids), and how many it keeps on purpose."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    dates = _email_dates(originals)

    def old(item) -> bool:
        d = dates.get(item.get("ref")) or _created(item)
        return d is not None and d < cutoff

    note_ids = [n["id"] for n in notes.items if old(n)]
    todo_ids, kept_upcoming = [], 0
    for t in todos.items:
        if not old(t):
            continue
        sent = dates.get(t.get("ref")) or _created(t)
        due = parse_due(t.get("due"), sent.date() if sent else now.date())   # "Oct 3" = the one after the email
        if keep_upcoming and not t.get("done") and due and due >= now.date():
            kept_upcoming += 1
            continue
        todo_ids.append(t["id"])
    return {"days": days, "notes": note_ids, "todos": todo_ids, "kept_upcoming": kept_upcoming,
            "notes_total": len(notes.items), "todos_total": len(todos.items)}


def run(data_dir: Path, todos, notes, originals, days: int = 30, keep_upcoming: bool = True) -> dict[str, Any]:
    p = plan(todos, notes, originals, days, keep_upcoming)
    gone_n, gone_t = set(p["notes"]), set(p["todos"])
    if not gone_n and not gone_t:
        return {**p, "removed_notes": 0, "removed_todos": 0}
    folder = Path(data_dir) / "cleared"
    folder.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    batch = {"at": datetime.now().isoformat(timespec="seconds"), "days": days,
             "notes": [n for n in notes.items if n["id"] in gone_n],
             "todos": [t for t in todos.items if t["id"] in gone_t]}
    (folder / f"{stamp}.json").write_text(json.dumps(batch, indent=1, ensure_ascii=False))
    notes.items = [n for n in notes.items if n["id"] not in gone_n]
    todos.items = [t for t in todos.items if t["id"] not in gone_t]
    notes.save()
    notes.render()
    todos.save()
    return {**p, "removed_notes": len(gone_n), "removed_todos": len(gone_t), "batch": stamp}


def last_batch(data_dir: Path) -> dict[str, Any] | None:
    folder = Path(data_dir) / "cleared"
    files = sorted(folder.glob("*.json")) if folder.exists() else []
    if not files:
        return None
    b = json.loads(files[-1].read_text())
    return {"batch": files[-1].stem, "at": b["at"], "notes": len(b["notes"]), "todos": len(b["todos"])}


def _reid(back: list[dict], current: list[dict]) -> list[dict]:
    """Items made since the clean-up may have reused an id: give a restored item a fresh one then."""
    used = {x["id"] for x in current}
    top = max(used | {x["id"] for x in back}, default=0)
    out = []
    for x in back:
        if x["id"] in used:
            top += 1
            x = {**x, "id": top}
        used.add(x["id"])
        out.append(x)
    return out


def undo(data_dir: Path, todos, notes) -> dict[str, int]:
    """Put the most recent clean-up back (ids are kept, so links between notes and to-dos still work)."""
    folder = Path(data_dir) / "cleared"
    files = sorted(folder.glob("*.json")) if folder.exists() else []
    if not files:
        raise ValueError("There's no clean-up to undo.")
    b = json.loads(files[-1].read_text())
    back_n, back_t = _reid(b["notes"], notes.items), _reid(b["todos"], todos.items)
    notes.items = sorted(notes.items + back_n, key=lambda n: n["id"])
    todos.items = sorted(todos.items + back_t, key=lambda t: t["id"])
    notes.save()
    notes.render()
    todos.save()
    files[-1].rename(files[-1].with_suffix(".undone"))
    return {"notes": len(back_n), "todos": len(back_t)}
