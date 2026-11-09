"""Mailbox back-ends.

LocalMailbox  JSON files on disk (inbox / archive / trash). Used by the demo and tests.
IMAPMailbox   Any IMAP account; Gmail-aware (archive = remove Inbox label).
"""
from __future__ import annotations

import email as email_lib
import imaplib
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
import json
import re
import urllib.request
from email import policy
from html import unescape
from pathlib import Path
from typing import Iterable

from .models import Email
from .netutil import ssl_context


def _email_date(value: str) -> date | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).date()          # RFC 2822 (real mail)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(value[:10]).date()    # ISO (demo mail)
    except ValueError:
        return None


class Mailbox:
    """fetch_new returns (emails, total_waiting): the newest `limit` unhandled emails from the last
    `days` days (None = no age limit), newest first, plus how many matched in total.
    By default only unread mail; include_read=True takes everything in the inbox."""  # noqa
    simulated = True

    def fetch_new(self, limit: int = 30, days: int | None = None, exclude: set[str] = frozenset(),
                  include_read: bool = False) -> tuple[list[Email], int]: ...
    def archive(self, msg: Email) -> str: ...
    def delete(self, msg: Email) -> str: ...
    def keep(self, msg: Email) -> str:
        return "left in inbox"

    def mark_read(self, msg: Email) -> str:
        return "marked as read"

    def restore(self, msg: Email) -> str: ...

    def close(self) -> None:
        pass


# --------------------------------------------------------------------------- #
class LocalMailbox(Mailbox):
    simulated = True

    def __init__(self, folder: Path):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)

    def _load(self, name: str) -> list[dict]:
        p = self.folder / f"{name}.json"
        return json.loads(p.read_text() or "[]") if p.exists() else []

    def _save(self, name: str, items: list[dict]) -> None:
        (self.folder / f"{name}.json").write_text(json.dumps(items, indent=2, ensure_ascii=False))

    def load_inbox(self, emails: Iterable[Email]) -> None:
        self._save("inbox", [e.to_dict() for e in emails])
        self._save("archive", [])
        self._save("trash", [])

    def fetch_arrivals(self, include_read: bool = False) -> list[Email]:
        """Emails that landed in the inbox since fetch_new looked."""
        known = getattr(self, "_known", set())
        fresh = [Email.from_dict(d) for d in self._load("inbox") if d["id"] not in known]
        self._known = known | {e.id for e in fresh}
        return fresh

    def fetch_new(self, limit: int = 30, days: int | None = None, exclude: set[str] = frozenset(),
                  include_read: bool = False) -> tuple[list[Email], int]:
        self._known = {d["id"] for d in self._load("inbox")}
        emails = [Email.from_dict(d) for d in self._load("inbox")]
        emails = [e for e in emails if e.id not in exclude]
        if days:
            cutoff = date.today() - timedelta(days=days)
            emails = [e for e in emails if (d := _email_date(e.date)) is None or d >= cutoff]
        order = {e.id: i for i, e in enumerate(emails)}  # inbox order breaks same-day ties
        emails.sort(key=lambda e: (_email_date(e.date) or date.max, order[e.id]), reverse=True)
        return emails[:limit], len(emails)

    def _move(self, msg: Email, dest: str) -> None:
        inbox = self._load("inbox")
        keep = [d for d in inbox if d["id"] != msg.id]
        moved = [d for d in inbox if d["id"] == msg.id]
        self._save("inbox", keep)
        self._save(dest, self._load(dest) + moved)

    def archive(self, msg: Email) -> str:
        self._move(msg, "archive")
        return "archived"

    def delete(self, msg: Email) -> str:
        self._move(msg, "trash")
        return "moved to trash"

    def restore(self, msg: Email) -> str:
        for folder in ("archive", "trash"):
            items = self._load(folder)
            hit = [d for d in items if d["id"] == msg.id]
            if hit:
                self._save(folder, [d for d in items if d["id"] != msg.id])
                self._save("inbox", self._load("inbox") + hit)
                return f"moved back to inbox from {folder}"
        return "already in the inbox" if any(d["id"] == msg.id for d in self._load("inbox")) else "not found"

    def counts(self) -> dict[str, int]:
        return {n: len(self._load(n)) for n in ("inbox", "archive", "trash")}


# --------------------------------------------------------------------------- #
def _html_to_text(html: str) -> str:
    html = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>|</h\d>", "\n", html)
    html = re.sub(r"(?i)<a\s[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", r"\2 (\1)", html)
    text = unescape(re.sub(r"<[^>]+>", " ", html))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()


class IMAPMailbox(Mailbox):
    simulated = False
    KEEP_HEADERS = ("List-Unsubscribe", "List-Unsubscribe-Post", "List-Id", "Precedence", "Reply-To")

    def __init__(self, host: str, user: str, password: str, folder: str = "INBOX",
                 archive_folder: str | None = None, trash_folder: str | None = None, dry_run: bool = False):
        self.dry_run = dry_run
        self.simulated = dry_run
        self.folder = folder
        self._login = (host, user, password)   # kept in memory only, to reconnect during long sessions
        self._connect()
        caps = {c.decode().upper() if isinstance(c, bytes) else c.upper() for c in self.conn.capabilities}
        self.gmail = "X-GM-EXT-1" in caps
        self.can_move = "MOVE" in caps
        # Folder names depend on the account's language ("[Gmail]/Trash", "[Gmail]/Corbeille", "[Gmail]/Кошик"…),
        # so ask the server which folder is flagged as Trash / All Mail / Archive (RFC 6154).
        special = self._special_folders()
        self.archive_folder = archive_folder or special.get("all") or special.get("archive") or (
            "[Gmail]/All Mail" if self.gmail else "Archive")
        self.trash_folder = trash_folder or special.get("trash") or ("[Gmail]/Trash" if self.gmail else "Trash")

    def _connect(self) -> None:
        host, user, password = self._login
        self.conn = imaplib.IMAP4_SSL(host, ssl_context=ssl_context(), timeout=30)
        self.conn.login(user, password)
        self.conn.select(self.folder)

    def reconnect(self) -> None:
        """Long auto-mode sessions can outlive a connection (sleep, Wi-Fi change): open a fresh one."""
        try:
            self.conn.logout()
        except Exception:
            pass
        self._connect()

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _ok(resp, what: str):
        """Every server reply is checked — a refused move must never be reported as done."""
        typ, data = resp
        if typ != "OK":
            detail = data[0].decode(errors="replace") if data and isinstance(data[0], bytes) else str(data)
            raise RuntimeError(f"{what} was refused by the mail server ({detail})")
        return data

    def _special_folders(self) -> dict[str, str]:
        found: dict[str, str] = {}
        try:
            typ, data = self.conn.list()
        except Exception:
            return found
        for line in data or []:
            text = line.decode(errors="replace") if isinstance(line, bytes) else str(line)
            m = re.match(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', text)
            if not m:
                continue
            flags, name = m.group("flags"), m.group("name").strip()
            for flag, key in (("\\Trash", "trash"), ("\\All", "all"), ("\\Archive", "archive")):
                if flag.lower() in flags.lower():
                    found[key] = name.strip('"')
        return found

    def find_in_inbox(self, message_id: str) -> str | None:
        """UID of the message in the inbox, or None if it isn't there (used to repair earlier moves)."""
        typ, data = self.conn.uid("SEARCH", None, "HEADER", "Message-ID", f'"{message_id}"')
        uids = data[0].split() if typ == "OK" and data and data[0] else []
        return uids[0].decode() if uids else None

    def fetch_new(self, limit: int = 30, days: int | None = None, exclude: set[str] = frozenset(),
                  include_read: bool = False) -> tuple[list[Email], int]:
        criteria = ["ALL"] if include_read else ["UNSEEN"]   # ALL = read and unread
        if days:
            criteria += ["SINCE", (date.today() - timedelta(days=days)).strftime("%d-%b-%Y")]
        typ, data = self.conn.uid("SEARCH", None, *criteria)
        uids = list(reversed(data[0].split())) if data and data[0] else []   # newest first
        typ, alld = self.conn.uid("SEARCH", None, "ALL")
        every = [int(u) for u in (alld[0].split() if alld and alld[0] else [])]
        self._high_uid = max(every, default=0)   # anything above this arrives later
        # cheap header pass: drop mail already handled in an earlier session (e.g. "keep")
        mids: dict[bytes, str] = {}
        for i in range(0, len(uids), 200):  # one request per 200 messages
            batch = b",".join(uids[i:i + 200])
            typ, hdrs = self.conn.uid("FETCH", batch, "(UID BODY.PEEK[HEADER.FIELDS (MESSAGE-ID)])")
            for part in hdrs or []:
                if isinstance(part, tuple):
                    m = re.search(rb"UID (\d+)", part[0])
                    if m:
                        mid = email_lib.message_from_bytes(part[1], policy=policy.default).get("Message-ID")
                        mids[m.group(1)] = str(mid) if mid else ""
        waiting = [uid for uid in uids if not (mids.get(uid) and mids[uid] in exclude)]
        return self._fetch_full(waiting[:limit]), len(waiting)

    def fetch_arrivals(self, include_read: bool = False) -> list[Email]:
        """Mail that reached the inbox since the session started (UIDs above the highest one seen)."""
        high = getattr(self, "_high_uid", None)
        if high is None:
            return []
        self.conn.noop()  # lets the server tell us about new messages
        criteria = ["UID", f"{high + 1}:*"] + ([] if include_read else ["UNSEEN"])
        typ, data = self.conn.uid("SEARCH", None, *criteria)
        uids = [u for u in (data[0].split() if typ == "OK" and data and data[0] else []) if int(u) > high]
        if not uids:
            return []
        self._high_uid = max(int(u) for u in uids)
        return self._fetch_full(list(reversed(uids)))

    def _fetch_full(self, uids) -> list[Email]:
        out = []
        for uid in uids:
            typ, msg_data = self.conn.uid("FETCH", uid, "(BODY.PEEK[])")  # PEEK: don't mark as read
            raw = next((part[1] for part in msg_data if isinstance(part, tuple)), None)
            if not raw:
                continue
            msg = email_lib.message_from_bytes(raw, policy=policy.default)
            body_part = msg.get_body(preferencelist=("plain", "html"))
            body = ""
            if body_part is not None:
                body = body_part.get_content()
                if body_part.get_content_type() == "text/html":
                    body = _html_to_text(body)
            out.append(Email(
                id=str(msg.get("Message-ID") or f"uid-{uid.decode()}"),
                uid=uid.decode(), raw=raw, sender=str(msg.get("From", "")), subject=str(msg.get("Subject", "")),
                date=str(msg.get("Date", "")), body=body,
                headers={h: str(msg[h]) for h in self.KEEP_HEADERS if msg[h]},
            ))
        return out

    def _move(self, msg: Email, folder: str) -> None:
        self._move_uid(msg.uid, folder)

    def _move_uid(self, uid, folder: str) -> None:
        if self.can_move:
            self._ok(self.conn.uid("MOVE", uid, f'"{folder}"'), f"Moving to {folder}")
        else:
            self._ok(self.conn.uid("COPY", uid, f'"{folder}"'), f"Copying to {folder}")
            self._ok(self.conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)"), "Removing from inbox")
            self.conn.expunge()

    def mark_read(self, msg: Email) -> str:
        if self.dry_run:
            return "would mark as read (dry run)"
        self._ok(self.conn.uid("STORE", msg.uid, "+FLAGS", r"(\Seen)"), "Marking as read")
        return "marked as read"

    def restore(self, msg: Email) -> str:
        """Find the message by Message-ID in Archive/Trash and put it back in the inbox."""
        if self.dry_run:
            return "would restore to inbox (dry run)"
        try:
            for folder in (self.archive_folder, self.trash_folder):
                if self.conn.select(f'"{folder}"')[0] != "OK":
                    continue
                typ, data = self.conn.uid("SEARCH", None, "HEADER", "Message-ID", f'"{msg.id}"')
                uids = data[0].split() if data and data[0] else []
                if not uids:
                    continue
                if self.gmail and folder == self.archive_folder:
                    self._ok(self.conn.uid("STORE", uids[0], "+X-GM-LABELS", r"(\Inbox)"), "Putting back")
                else:
                    self._move_uid(uids[0], self.folder)
                return f"moved back to inbox from {folder}"
            return "not found on the server (Trash may have been emptied) — the saved copy is still available"
        finally:
            self.conn.select(self.folder)

    def archive(self, msg: Email) -> str:
        if self.dry_run:
            return "would archive (dry run)"
        if self.gmail:
            self._ok(self.conn.uid("STORE", msg.uid, "-X-GM-LABELS", r"(\Inbox)"), "Archiving")
        else:
            self._move(msg, self.archive_folder)
        return "archived"

    def delete(self, msg: Email) -> str:
        if self.dry_run:
            return "would delete (dry run)"
        try:
            self._move(msg, self.trash_folder)
        except RuntimeError:
            if not self.gmail:
                raise
            # Gmail's own way, independent of folder names: give it the Trash label
            self._ok(self.conn.uid("STORE", msg.uid, "+X-GM-LABELS", r"(\Trash)"), "Moving to Trash")
        return "moved to trash"

    def keep(self, msg: Email) -> str:
        if not self.dry_run:
            self._ok(self.conn.uid("STORE", msg.uid, "+FLAGS", r"(\Flagged)"), "Flagging")
        return "left in inbox (flagged)"

    def close(self) -> None:
        try:
            self.conn.logout()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
def unsubscribe_targets(msg: Email) -> tuple[list[str], list[str]]:
    """(https links, mailto links) from List-Unsubscribe, else from the body."""
    header = msg.header("List-Unsubscribe") or ""
    items = re.findall(r"<([^>]+)>", header) or [h.strip() for h in header.split(",") if h.strip()]
    if not items:
        items = [u for u in re.findall(r"https?://[^\s<>\")]+", msg.body) if "unsub" in u.lower()]
    https = [i for i in items if i.lower().startswith("http")]
    mailto = [i for i in items if i.lower().startswith("mailto:")]
    return https, mailto


def unsubscribe(msg: Email, simulated: bool) -> tuple[bool, str, str | None]:
    """Returns (done, message, manual_link). RFC 8058 one-click when offered."""
    https, mailto = unsubscribe_targets(msg)
    one_click = "one-click" in (msg.header("List-Unsubscribe-Post") or "").lower()
    if simulated:
        where = https[0] if https else (mailto[0] if mailto else "no unsubscribe link found")
        return bool(https or mailto), f"unsubscribed (simulated) via {where}", None
    if https and one_click:
        try:
            req = urllib.request.Request(https[0], data=b"List-Unsubscribe=One-Click", method="POST",
                                         headers={"Content-Type": "application/x-www-form-urlencoded"})
            with urllib.request.urlopen(req, timeout=15, context=ssl_context()) as resp:
                if resp.status < 400:
                    return True, "unsubscribed (one-click)", None
        except Exception as exc:
            return False, f"one-click unsubscribe failed ({exc})", https[0]
    link = https[0] if https else (mailto[0] if mailto else None)
    if link:
        return False, "needs a manual click — added to your to-dos", link
    return False, "no unsubscribe link found", None
