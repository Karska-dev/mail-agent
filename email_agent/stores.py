"""Where extracted information goes: notes, to-dos and an activity log."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import EXTRACT_FIELDS


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class JsonList:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.items: list[dict[str, Any]] = json.loads(self.path.read_text() or "[]") if self.path.exists() else []

    def append(self, item: dict[str, Any]) -> dict[str, Any]:
        self.items.append(item)
        self.save()
        return item

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(self.items, indent=2, ensure_ascii=False))


class Events(JsonList):
    """Things that happen outside a session but matter for Insights: put-backs and rule deletions."""

    def record(self, kind: str, **fields: Any) -> dict[str, Any]:
        return self.append({"at": _now(), "type": kind, **fields})


def _fmt(value: Any) -> str:
    if isinstance(value, list):
        return "; ".join(str(v) for v in value)
    return str(value)


class Notes(JsonList):
    """notes.json (data) + notes.md (human-readable, regenerated on every write).
    A note you edit keeps your text in "text", which then replaces the extracted items."""

    def __init__(self, path: Path):
        super().__init__(path)
        for i, n in enumerate(self.items, 1):  # older notes had no id
            n.setdefault("id", i)

    def _next_id(self) -> int:
        return max((n.get("id", 0) for n in self.items), default=0) + 1

    def add(self, source: str, subject: str, email_id: str, category: str, items: dict[str, Any], summary: str,
            ref: int | None = None, title: str | None = None, link: str | None = None,
            link_label: str | None = None):
        note = self.append({"id": self._next_id(), "created": _now(), "source": source, "subject": subject,
                            "email_id": email_id, "ref": ref, "category": category, "items": items,
                            "summary": summary, "title": title or None, "link": link or None,
                            "link_label": link_label or None})
        self.render()
        return note

    def as_text(self, n: dict[str, Any]) -> str:
        if n.get("text") is not None:
            return n["text"]
        if not n["items"]:
            return n.get("summary", "")
        return "\n".join(f"{EXTRACT_FIELDS.get(k, k).capitalize()}: {_fmt(v)}" for k, v in n["items"].items())

    def edit(self, note_id: int, text: str, title: str | None = None) -> bool:
        for n in self.items:
            if n.get("id") == note_id:
                n["text"] = text.strip()
                if title is not None and title.strip():
                    n["title"] = title.strip()
                n["edited"] = _now()
                self.save()
                self.render()
                return True
        return False

    def delete(self, note_id: int) -> bool:
        before = len(self.items)
        self.items = [n for n in self.items if n.get("id") != note_id]
        self.save()
        self.render()
        return len(self.items) < before

    def render(self) -> None:
        lines = ["# Notes\n", "_Saved automatically by the email agent. `#N` = see the original with "
                 "`python -m email_agent show N`._\n"]
        for n in reversed(self.items):
            ref = f" · original #{n['ref']}" if n.get("ref") else ""
            lines.append(f"## {n.get('title') or n['source'] + ' — ' + n['subject']}")
            lines.append(f"_{n['created'][:10]} · {n['category']}{ref}{' · edited' if n.get('edited') else ''}_\n")
            if n.get("text") is not None:
                lines += [f"{line}  " for line in n["text"].splitlines()]
            elif not n["items"]:
                lines.append(f"- {n['summary']}")
            if n.get("text") is None:
                for k, v in n["items"].items():
                    lines.append(f"- **{EXTRACT_FIELDS.get(k, k).capitalize()}:** {_fmt(v)}")
            lines.append("")
        self.path.with_suffix(".md").write_text("\n".join(lines))


class Todos(JsonList):
    def add(self, title: str, due: str | None, source: str, details: dict[str, Any], email_id: str,
            ref: int | None = None, link: str | None = None, link_label: str | None = None,
            about: str | None = None):
        next_id = max((t["id"] for t in self.items), default=0) + 1
        return self.append({"id": next_id, "title": title, "due": due, "source": source,
                            "details": details, "email_id": email_id, "ref": ref, "done": False,
                            "created": _now(), "link": link or None, "link_label": link_label or None,
                            "about": about or None})

    def update(self, todo_id: int, **fields) -> bool:
        for t in self.items:
            if t["id"] == todo_id:
                t.update(fields)
                self.save()
                return True
        return False

    def _set_done(self, todo_id: int, done: bool) -> bool:
        for t in self.items:
            if t["id"] == todo_id:
                t["done"] = done
                t["done_at"] = _now() if done else None
                self.save()
                return True
        return False

    def complete(self, todo_id: int) -> bool:
        return self._set_done(todo_id, True)

    def reopen(self, todo_id: int) -> bool:
        return self._set_done(todo_id, False)

    def delete(self, todo_id: int) -> bool:
        before = len(self.items)
        self.items = [t for t in self.items if t["id"] != todo_id]
        self.save()
        return len(self.items) < before


class ReadLog(JsonList):
    """Emails you chose to 'just read & archive': a short digest of each, no rule attached."""

    def add(self, ref: int | None, source: str, subject: str, date: str, summary: str, facts: dict[str, Any]):
        entry = self.append({"ref": ref, "read_at": _now(), "source": source, "subject": subject,
                             "date": date, "summary": summary, "facts": facts})
        self.render()
        return entry

    def render(self) -> None:
        lines = ["# Read & archived\n", "_One-off emails the agent read for you and archived without a rule._\n"]
        for e in reversed(self.items):
            lines.append(f"## {e['source']} — {e['subject']}")
            lines.append(f"_{(e['date'] or e['read_at'])[:10]} · original #{e['ref']}_\n")
            lines.append(e["summary"])
            for k, v in e["facts"].items():
                lines.append(f"- **{EXTRACT_FIELDS.get(k, k).capitalize()}:** {_fmt(v)}")
            lines.append("")
        self.path.with_suffix(".md").write_text("\n".join(lines))


class Originals:
    """A local copy of every email the agent handles, so you can always review the original.

    originals/index.json   one row per email: ref number, sender, subject, where it is now, what handled it
    originals/00012.json   parsed email (headers, full text body)
    originals/00012.eml    raw message as received (IMAP only) — opens in any mail app
    """

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.index_path = self.folder / "index.json"
        self.index: list[dict[str, Any]] = (json.loads(self.index_path.read_text() or "[]")
                                            if self.index_path.exists() else [])

    def _save_index(self) -> None:
        self.index_path.write_text(json.dumps(self.index, indent=2, ensure_ascii=False))

    def ref_for(self, email_id: str) -> int | None:
        return next((e["ref"] for e in self.index if e["email_id"] == email_id), None)

    def save(self, email, account: str | None = None) -> int:
        ref = self.ref_for(email.id)
        if ref is not None:
            return ref
        ref = max((e["ref"] for e in self.index), default=0) + 1
        (self.folder / f"{ref:05d}.json").write_text(json.dumps(email.to_dict(), indent=2, ensure_ascii=False))
        if email.raw:
            (self.folder / f"{ref:05d}.eml").write_bytes(email.raw)
        self.index.append({"ref": ref, "email_id": email.id, "from": email.sender, "subject": email.subject,
                           "date": email.date, "saved": _now(), "location": "inbox", "handled_by": None,
                           "has_eml": bool(email.raw), "account": account})
        self._save_index()
        return ref

    def entry(self, ref: int) -> dict[str, Any] | None:
        return next((e for e in self.index if e["ref"] == ref), None)

    def load(self, ref: int):
        from .models import Email
        path = self.folder / f"{ref:05d}.json"
        return Email.from_dict(json.loads(path.read_text())) if path.exists() else None

    def eml_path(self, ref: int) -> Path | None:
        p = self.folder / f"{ref:05d}.eml"
        return p if p.exists() else None

    def update(self, ref: int | None, **fields) -> None:
        e = self.entry(ref) if ref is not None else None
        if e:
            e.update(fields)
            self._save_index()

    def search(self, text: str | None = None) -> list[dict[str, Any]]:
        if not text:
            return list(self.index)
        t = text.lower()
        return [e for e in self.index if t in e["from"].lower() or t in e["subject"].lower()]


class Accounts(JsonList):
    """Saved mailboxes (no passwords here — those live in the Keychain, see keychain.py)."""

    def find(self, user: str) -> dict[str, Any] | None:
        return next((a for a in self.items if a["user"].lower() == user.lower()), None)

    def upsert(self, user: str, host: str) -> dict[str, Any]:
        acc = self.find(user)
        if acc:
            acc["host"] = host
            self.save()
            return acc
        return self.append({"user": user, "host": host, "added": _now()})

    def remove(self, user: str) -> None:
        self.items = [a for a in self.items if a["user"].lower() != user.lower()]
        self.save()
