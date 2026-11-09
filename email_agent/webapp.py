"""Local web app: `python3 -m email_agent app` opens the agent in your browser.

Runs only on your own computer (127.0.0.1). The Gmail app password you type is kept in
memory for this app session and never written to disk.
"""
from __future__ import annotations

import json
from datetime import datetime
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import traceback
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .agent import EmailAgent
from .demo import DEMO_EMAILS
from . import cleanup, keychain, local_ai, render
from .llm import DEFAULT_MODEL, check_key, claude_available, make_engines
from .mailbox import IMAPMailbox, LocalMailbox
from .models import EXTRACT_FIELDS, describe_actions
from .rules import SHORT, RuleStore
from .stores import Accounts, Notes, Originals, ReadLog, Todos
from .ui import STOP_RE, StopSession

WEB_DIR = Path(__file__).parent / "web"
DEMO_DIR = Path(tempfile.gettempdir()) / "email_agent_demo"
_STOP = object()
API_KEY_NAME = "anthropic-api-key"


def _fmt(v: Any) -> str:
    return "; ".join(map(str, v)) if isinstance(v, list) else str(v)


# --------------------------------------------------------------------------- #
# UI adapter: the agent talks to this exactly like it talks to the terminal.
# --------------------------------------------------------------------------- #
class WebUI:
    def __init__(self):
        self.lock = threading.Lock()
        self.events: list[dict[str, Any]] = []
        self.question: dict[str, Any] | None = None
        self.answers: queue.Queue = queue.Queue()
        self.stop_requested = False
        self._qid = 0
        self.activity: str | None = "Starting…"   # what the agent is busy with (None = waiting for you)
        self.activity_since = time.time()

    def busy(self, text: str | None) -> None:
        with self.lock:
            self.activity = text
            self.activity_since = time.time()

    def _emit(self, **event) -> None:
        with self.lock:
            event["seq"] = len(self.events)
            self.events.append(event)

    # --- output -------------------------------------------------------------
    def info(self, text: str = "") -> None:
        if text.strip():
            self._emit(type="info", text=text.strip())

    def header(self, n, total, email, ref=None, fresh=False) -> None:
        self._emit(type="email", n=n, total=total, subject=email.subject, sender=email.sender,
                   date=email.date, ref=ref, fresh=fresh)

    def show_analysis(self, a) -> None:
        facts = {EXTRACT_FIELDS.get(k, k): v for k, v in a.values().items()
                 if k not in ("links", "dates", "action_items", "amounts")}
        self._emit(type="analysis", category=a.category, summary=a.summary, facts=facts,
                   confidence=a.confidence, engine=a.engine, importance=a.importance,
                   importance_reason=a.importance_reason, risk=a.risk, risk_signs=a.risk_signs,
                   seconds=a.seconds)

    def show_full(self, email, ref=None) -> None:
        self._emit(type="original", ref=ref)

    def show_reading(self, email, ref=None) -> None:
        _, info = render.email_page(email.raw, email.body)
        self._emit(type="reading", ref=ref, body=email.body, sender=email.sender, date=email.date, view=info)

    def done(self, text: str) -> None:
        self._emit(type="done", text=text)

    def notice(self, text: str) -> None:
        self._emit(type="notice", text=text)

    # --- questions ----------------------------------------------------------
    def _wait(self, q: dict[str, Any]):
        with self.lock:
            self._qid += 1
            q["id"] = self._qid
            self.question = q
            self.activity = None
        if self.stop_requested:
            ans = _STOP
        else:
            ans = self.answers.get()
        with self.lock:
            self.question = None
        if ans is _STOP:
            raise StopSession
        return ans

    def ask_text(self, prompt: str) -> str:
        ans = self._wait({"kind": "text", "prompt": prompt})
        if STOP_RE.match(ans):
            raise StopSession
        return ans

    def choose(self, prompt: str, options: list[str]) -> int:
        return int(self._wait({"kind": "choice", "prompt": prompt, "options": options}))

    # --- from the browser ---------------------------------------------------
    def answer(self, qid: int, value: str) -> bool:
        with self.lock:
            q = self.question
            if not q or q["id"] != qid:
                return False
            label = {"r": "Just read it & archive — no rule", "skip": "Skip for now"}.get(value, value)
            if q["kind"] == "choice":
                label = q["options"][int(value)]
            self.question = {**q, "answered": True}
        self._emit(type="answer", text=label)
        self.busy("Working on it…")
        self.answers.put(value)
        return True

    def request_stop(self) -> None:
        self.stop_requested = True
        self.busy("Stopping after this step…")
        self.answers.put(_STOP)


# --------------------------------------------------------------------------- #
def _friendly_error(exc: Exception) -> str:
    msg = str(exc)
    up = msg.upper()
    if "AUTHENTICATIONFAILED" in up or "INVALID CREDENTIALS" in up or "LOGIN FAILED" in up:
        return ("The mail server didn't accept this email + app password. For Gmail use an App Password "
                "(myaccount.google.com/apppasswords), not your normal password.")
    if "CERTIFICATE_VERIFY_FAILED" in up:
        return ("Your Python can't verify secure connections. In Finder open Applications → Python 3.x and "
                "double-click “Install Certificates.command”, then try again.")
    if "NODENAME NOR SERVNAME" in up or "NAME OR SERVICE NOT KNOWN" in up or "TIMED OUT" in up:
        return "Couldn't reach the mail server. Check your internet connection and the server name."
    return msg


class App:
    def __init__(self, data_dir: Path, engine: str, model: str):
        self.last_seen = time.monotonic()   # last request from the page
        self.request_quit = None   # set by serve(): shuts the server down
        self.real_dir = Path(data_dir).expanduser()
        self.real_dir.mkdir(parents=True, exist_ok=True)
        self.engine = engine
        self.model = model
        self.accounts = Accounts(self.real_dir / "accounts.json")
        self.mode = "gmail" if self.accounts.items else "demo"
        self.ui: WebUI | None = None
        self.agent: EmailAgent | None = None
        self.status = "idle"          # idle | connecting | running | finished | error
        self.stats: dict[str, int] = {}
        self.error: str | None = None
        self.dry_run = False
        self.include_read = False
        self.auto = False              # auto mode: whole inbox, no questions, skip what needs you
        self.reading: list[str] = []  # accounts in the current session
        self.current_account: str | None = None
        self.thread: threading.Thread | None = None
        self.opened_at = datetime.now().isoformat(timespec="seconds")  # to-dos finished before this are hidden
        self.settings_path = self.real_dir / "settings.json"
        self.settings = {"reader": "builtin", "local_model": None}
        if self.settings_path.exists():
            self.settings.update(json.loads(self.settings_path.read_text() or "{}"))
        self.pull_state: dict[str, Any] | None = None
        if not os.environ.get("ANTHROPIC_API_KEY"):
            saved = keychain.get(API_KEY_NAME)
            if saved:
                os.environ["ANTHROPIC_API_KEY"] = saved

    @property
    def data_dir(self) -> Path:
        return DEMO_DIR if self.mode == "demo" else self.real_dir

    # ------------------------------------------------------------------ accounts
    def _password(self, user: str) -> str:
        pw = keychain.get(f"imap:{user.lower()}")
        if not pw:
            raise ValueError(f"No saved password for {user}. Remove the mailbox and add it again.")
        return pw

    def add_account(self, user: str, password: str, host: str) -> dict[str, Any]:
        user, host, password = user.strip(), (host or "imap.gmail.com").strip(), password.replace(" ", "").strip()
        if not (user and password):
            raise ValueError("Enter the email address and its app password.")
        try:  # check it works before saving anything
            IMAPMailbox(host, user, password, dry_run=True).close()
        except Exception as exc:
            raise ValueError(_friendly_error(exc)) from None
        keychain.put(f"imap:{user.lower()}", password)
        acc = self.accounts.upsert(user, host)
        if self.status not in ("connecting", "running"):
            self.set_mode("gmail")
        return acc

    def remove_account(self, user: str) -> None:
        keychain.delete(f"imap:{user.lower()}")
        self.accounts.remove(user)

    def set_api_key(self, key: str) -> None:
        key = key.strip()
        if not key:
            raise ValueError("Paste your API key.")
        try:
            check_key(key, self.model)
        except Exception as exc:
            raise ValueError(f"That key didn't work: {_friendly_error(exc)}") from None
        keychain.put(API_KEY_NAME, key)
        os.environ["ANTHROPIC_API_KEY"] = key
        self.set_reader("claude")

    def remove_api_key(self) -> None:
        keychain.delete(API_KEY_NAME)
        os.environ.pop("ANTHROPIC_API_KEY", None)
        if self.settings.get("reader") == "claude":
            self.set_reader("builtin")

    # ------------------------------------------------------------------ which AI reads the mail
    def _save_settings(self) -> None:
        self.settings_path.write_text(json.dumps(self.settings, indent=2))

    def set_reader(self, reader: str, local_model: str | None = None) -> None:
        if reader not in ("builtin", "local", "claude"):
            raise ValueError("Unknown reader.")
        if reader == "claude" and not claude_available():
            raise ValueError("Save an Anthropic API key first.")
        self.settings["reader"] = reader
        if local_model:
            self.settings["local_model"] = local_model
        self._save_settings()

    def local_model(self) -> str:
        return self.settings.get("local_model") or local_ai.recommended_model()

    def _engines(self):
        """Pick the reader from settings, falling back to the built-in one (with a note) if unavailable."""
        if self.engine != "auto":  # forced from the command line
            return make_engines(self.engine, self.model, self.settings.get("local_model"))
        reader = self.settings.get("reader", "builtin")
        if self.pull_state and self.pull_state.get("active"):
            self.ui.notice(f"The local model {self.pull_state['model']} is still downloading. "
                           "Using the built-in reader this time.")
            return make_engines("offline")
        if reader == "local":
            st, model = local_ai.status(), self.local_model()
            have = {m.removesuffix(":latest") for m in st["models"]} | set(st["models"])
            if not st["running"]:
                self.ui.notice("Local AI is off — open the Ollama app to use it. Using the built-in reader this time.")
            elif model not in have:
                self.ui.notice(f"The local model {model} isn't downloaded yet. Using the built-in reader this time.")
            else:
                local_ai.warm_up(model)  # start loading the model while we connect to the mailbox
                return make_engines("local", self.model, model)
        elif reader == "claude":
            if claude_available():
                return make_engines("claude", self.model)
            self.ui.notice("No Anthropic API key saved. Using the built-in reader.")
        return make_engines("offline")

    def start_pull(self, model: str, update: bool = False) -> None:
        if self.pull_state and self.pull_state.get("active"):
            raise ValueError("A download is already running.")
        model = (model or "").strip()
        if not re.fullmatch(r"[\w.\-/]+(:[\w.\-]+)?", model):
            raise ValueError("That doesn't look like a model name (example: qwen2.5:7b).")
        if not local_ai.status()["running"]:
            raise ValueError("Open the Ollama app first, then try again.")
        self.pull_state = {"model": model, "active": True, "status": "checking…" if update else "starting…",
                           "completed": 0, "total": 0, "update": update, "downloaded": 0}

        def work():
            try:
                def progress(msg):
                    done, total = msg.get("completed", 0) or 0, msg.get("total", 0) or 0
                    if total and done < total:          # a layer actually being downloaded = there was an update
                        self.pull_state["downloaded"] = max(self.pull_state.get("downloaded", 0), total)
                    self.pull_state.update(status=msg.get("status", ""), completed=done,
                                           total=total or self.pull_state.get("total", 0))
                local_ai.pull(model, progress)
                self.pull_state.update(active=False, status="done", done=True)
                self.set_reader("local", model)
            except Exception as exc:
                msg = str(exc)
                if "file does not exist" in msg or "not found" in msg.lower():
                    msg = f"Ollama doesn't have a model called “{model}”. Check the name on ollama.com/library."
                self.pull_state.update(active=False, error=msg)
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ sessions
    def start(self, params: dict[str, Any]) -> None:
        if self.status in ("connecting", "running"):
            raise ValueError("A session is already running.")
        self.mode = params.get("mode", "demo")
        if self.mode == "gmail":
            chosen = [u for u in params.get("accounts", []) if self.accounts.find(u)]
            if not chosen:
                raise ValueError("Pick at least one mailbox to read.")
            self.reading = chosen
        self.dry_run = bool(params.get("dry_run")) and self.mode == "gmail"
        self.ui = WebUI()
        self.agent = None
        self.stats, self.error, self.status = {}, None, "connecting"
        self._counted: dict[str, int] = {}   # totals from mailboxes already finished this session
        days = int(params.get("days") or 0) or None
        limit = int(params.get("limit") or 30)
        self.include_read = bool(params.get("include_read")) and self.mode == "gmail"
        self.auto = bool(params.get("auto"))
        self.thread = threading.Thread(target=self._run, args=(days, limit), daemon=True)
        self.thread.start()

    def _run(self, days, limit) -> None:
        try:
            analyzer, parser = self._engines()
            if self.mode == "demo":
                keep = None   # demo auto mode reuses the rules you taught in the last demo
                if self.auto and (DEMO_DIR / "rules.json").exists():
                    keep = (DEMO_DIR / "rules.json").read_text()
                if DEMO_DIR.exists():
                    shutil.rmtree(DEMO_DIR)
                if keep:
                    DEMO_DIR.mkdir(parents=True, exist_ok=True)
                    (DEMO_DIR / "rules.json").write_text(keep)
                elif self.auto:
                    self.ui.info("No demo rules yet — run the normal demo first and answer a few questions; "
                                 "auto mode then uses those rules. For now everything will be skipped.")
                self.ui.busy("Preparing the demo inbox…")
                mailbox = LocalMailbox(DEMO_DIR / "mailbox")
                mailbox.load_inbox(DEMO_EMAILS)
                self.agent = EmailAgent(self.data_dir, mailbox, analyzer, parser, self.ui)
                self.status = "running"
                self.stats = (self.agent.run_auto(watch_arrivals=False) if self.auto
                              else self.agent.run(limit=limit, days=None))
                self.status = "finished"
                return
            totals: dict[str, int] = {}
            if self.dry_run:
                self.ui.info("Practice mode: nothing in your mailboxes will be moved, deleted or marked as read.")
            # `limit` is the whole session: split it evenly across the chosen mailboxes; a mailbox that
            # needs fewer than its share hands the spare slots on to the ones after it.
            budget = 10 ** 9 if self.auto else limit
            if self.auto:
                self.ui.info("Auto mode: going through the whole inbox of "
                             + ", ".join(self.reading) + " — no questions; anything that needs you stays unread.")
            elif len(self.reading) > 1:
                share0 = -(-limit // len(self.reading))
                self.ui.info(f"{limit} emails this session, split across {len(self.reading)} mailboxes "
                             f"(about {share0} each; unused slots move on to the next mailbox).")
            for i, user in enumerate(self.reading):
                if self.ui.stop_requested or budget <= 0:
                    break
                share = -(-budget // (len(self.reading) - i))   # ceil(budget / mailboxes left)
                acc = self.accounts.find(user)
                self.current_account = user
                self.ui._emit(type="account", user=user)
                self.ui.busy(f"Connecting to {user}…")
                try:
                    mailbox = IMAPMailbox(acc["host"], user, self._password(user), dry_run=self.dry_run)
                except Exception as exc:
                    self.ui.notice(f"Couldn't open {user}: {_friendly_error(exc)}")
                    continue
                try:
                    self.agent = EmailAgent(self.data_dir, mailbox, analyzer, parser, self.ui, account=user)
                    self.status = "running"
                    if self.auto:
                        stats = self.agent.run_auto(include_read=self.include_read)
                    else:
                        stats = self.agent.run(limit=share, days=days, include_read=self.include_read,
                                               newest_first=True, watch_arrivals=True)
                finally:
                    mailbox.close()
                budget -= sum(stats.get(k, 0) for k in ("auto", "asked", "one_off", "skipped"))
                for k, v in stats.items():
                    totals[k] = totals.get(k, 0) + v
                self._counted = dict(totals)
                self.agent.live_stats = None
                if stats.get("stopped"):
                    break
            self.stats = totals
            self.status = "finished"
        except Exception as exc:  # show it in the browser rather than dying silently
            self.error = _friendly_error(exc)
            self.status = "error"
            traceback.print_exc()

    def _live(self) -> dict[str, int]:
        """Running counts for the status bar: handled automatically, skipped, decided by you…"""
        out = dict(getattr(self, "_counted", {}) or {})
        cur = getattr(self.agent, "live_stats", None) if self.agent else None
        for k, v in (cur or {}).items():
            out[k] = out.get(k, 0) + v
        return {k: out.get(k, 0) for k in ("auto", "skipped", "asked", "one_off")}

    def stop(self) -> None:
        if self.ui:
            self.ui.request_stop()

    # ------------------------------------------------------------------ #
    def state(self, since: int) -> dict[str, Any]:
        ui = self.ui
        events, question = [], None
        if ui:
            with ui.lock:
                events = ui.events[since:]
                q = ui.question
                question = None if (not q or q.get("answered")) else dict(q)
        activity, secs = None, 0
        if ui and self.status in ("connecting", "running") and not question:
            with ui.lock:
                activity, secs = ui.activity or "Working…", int(time.time() - ui.activity_since)
        return {"status": self.status, "mode": self.mode, "events": events, "question": question,
                "activity": activity, "activity_seconds": secs,
                "stats": self.stats, "error": self.error, "dry_run": self.dry_run, "auto": self.auto, "live": self._live(),
                "reading": self.reading if self.mode == "gmail" else [],
                "current_account": self.current_account if self.mode == "gmail" else None,
                "accounts": self.accounts.items, "keychain": keychain.where(),
                "claude": claude_available(), "reader": self.settings.get("reader", "builtin"),
                "local_model": self.local_model(), "pull": self.pull_state,
                "local": local_ai.status() if self.status != "running" else None,
                "local_choices": [{"model": m, "size": sz, "note": n, "new": new}
                                  for m, sz, n, new in local_ai.RECOMMENDED],
                "memory_gb": local_ai.mac_memory_gb()}

    # stores: live objects while a session runs, otherwise fresh from disk
    def _stores(self):
        a = self.agent
        if a and a.data_dir == self.data_dir:
            return a.todos, a.notes, a.rules, a.read_log, a.originals
        d = self.data_dir
        return (Todos(d / "todos.json"), Notes(d / "notes.json"), RuleStore(d / "rules.json"),
                ReadLog(d / "read_log.json"), Originals(d / "originals"))

    def panels(self) -> dict[str, Any]:
        """Sidebar data. Each list is built separately so one problem can't blank the whole sidebar."""
        todos, notes, rules, read_log, originals = self._stores()
        builders = self._panel_builders(todos, notes, rules, read_log, originals)
        out: dict[str, Any] = {"errors": {}}
        for name, build in builders.items():
            try:
                out[name] = build()
            except Exception as exc:
                traceback.print_exc()
                out[name] = []
                out["errors"][name] = str(exc)
        return out

    def _sent_dates(self, originals) -> dict:
        key = (id(originals), len(originals.index))
        if getattr(self, "_dates_key", None) != key:      # email dates, parsed once per change
            self._dates_key, self._dates = key, cleanup._email_dates(originals)
        return self._dates

    def _sent(self, item, dates):
        """When the email behind a note/to-do was sent (falls back to when the item was made)."""
        return dates.get(item.get("ref")) or cleanup._created(item)

    @staticmethod
    def _newest_first(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(rows, key=lambda r: (r.get("sent") or "", r.get("id", 0)), reverse=True)

    def _todo_rows(self, todos, originals) -> list[dict[str, Any]]:
        from datetime import date, timezone
        dates = self._sent_dates(originals)
        today = date.today()
        rows = []
        for t in todos.items:
            sent = self._sent(t, dates)
            rows.append({**t, "hidden": bool(t.get("done")) and (t.get("done_at") or "") < self.opened_at,
                         "when": cleanup.todo_when(t, sent.date() if sent else None, today),
                         "sent": sent.astimezone(timezone.utc).isoformat() if sent else None})
        return self._newest_first(rows)

    def _note_rows(self, todos, notes, originals) -> list[dict[str, Any]]:
        from datetime import timezone
        dates = self._sent_dates(originals)
        todo_ids = {t["id"] for t in todos.items}
        rows = []
        for n in notes.items:
            sent = self._sent(n, dates)
            rows.append({**n, "items": {EXTRACT_FIELDS.get(k, k): _fmt(v) for k, v in n["items"].items()},
                         "as_text": notes.as_text(n),
                         "title": n.get("title") or f"{n['source']} — {n['subject']}",
                         "in_todos": n.get("todo_id") in todo_ids,
                         "sent": sent.astimezone(timezone.utc).isoformat() if sent else None})
        return self._newest_first(rows)

    def _panel_builders(self, todos, notes, rules, read_log, originals):
        return {
            # finished to-dos stay (crossed out) while the app is open; once reopened they're tucked away
            "todos": lambda: self._todo_rows(todos, originals),
            "notes": lambda: self._note_rows(todos, notes, originals),
            "rules": lambda: [{"id": r.id, "name": r.name, "scope": r.scope, "category": r.category,
                               "requires": [SHORT.get(f, f) for f in r.required_features],
                               "asks_if": [SHORT.get(f, f) for f in r.excluded_features],
                               "actions": describe_actions(r.actions), "instruction": r.instruction,
                               "used": r.times_applied} for r in rules.rules],
            "read_log": lambda: list(reversed(read_log.items)),
            "history": lambda: list(reversed(originals.index)),
        }

    def original(self, ref: int) -> dict[str, Any]:
        originals = self._stores()[4]
        entry = originals.entry(ref)
        email = originals.load(ref)
        if not (entry and email):
            raise ValueError(f"No email #{ref}.")
        eml = originals.eml_path(ref)
        _, info = render.email_page(eml.read_bytes() if eml else None, email.body)
        return {"entry": entry, "email": email.to_dict(), "view": info}

    def original_page(self, ref: int, images: bool, plain: bool) -> bytes:
        originals = self._stores()[4]
        email = originals.load(ref)
        if not email:
            raise ValueError(f"No email #{ref}.")
        eml = originals.eml_path(ref)
        page, _ = render.email_page(eml.read_bytes() if eml else None, email.body, prefer_plain=plain)
        return page.encode()

    def todo_done(self, todo_id: int) -> None:
        self._stores()[0].complete(todo_id)

    # ------------------------------------------------------------------ AI rewrite of existing items
    def _reader_for_rewrite(self):
        reader = self.settings.get("reader", "builtin")
        if reader == "local":
            st, model = local_ai.status(), self.local_model()
            if st["running"] and (model in st["models"] or f"{model}:latest" in st["models"]):
                return local_ai.LocalAnalyzer(model)
            raise ValueError("Local AI is off — open the Ollama app first.")
        if reader == "claude" and claude_available():
            from .llm import ClaudeAnalyzer
            return ClaudeAnalyzer(self.model)
        from .analyzer import HeuristicAnalyzer
        return HeuristicAnalyzer()

    def _reread(self, ref: int | None):
        if not ref:
            raise ValueError("This one has no saved original email to re-read.")
        email = self._stores()[4].load(ref)
        if not email:
            raise ValueError(f"Original email #{ref} is missing.")
        return email, self._reader_for_rewrite().analyze(email)

    def rewrite_todo(self, todo_id: int) -> dict[str, Any]:
        from .agent import todo_title_and_due
        todos = self._stores()[0]
        t = next((x for x in todos.items if x["id"] == todo_id), None)
        if not t:
            raise ValueError("That to-do no longer exists.")
        email, a = self._reread(t.get("ref"))
        title, due = todo_title_and_due(a, email)
        todos.update(todo_id, title=title, due=due, link=a.key_link or None,
                     link_label=a.key_link_label or None, about=a.summary, rewritten=True)
        return {"title": title}

    def rewrite_note(self, note_id: int) -> dict[str, Any]:
        notes = self._stores()[1]
        n = next((x for x in notes.items if x.get("id") == note_id), None)
        if not n:
            raise ValueError("That note no longer exists.")
        email, a = self._reread(n.get("ref"))
        fresh = a.values(list(n["items"]) or None)
        n.update(title=a.note_title or n.get("title"), summary=a.summary, link=a.key_link or None,
                 link_label=a.key_link_label or None, rewritten=True)
        if fresh:
            n["items"] = {k: v for k, v in fresh.items() if k not in ("links", "amounts", "dates")} or n["items"]
        notes.save()
        notes.render()
        return {"title": n["title"]}

    def note_to_todo(self, note_id: int) -> dict[str, Any]:
        """Turn a note into a to-do: same title, first real date as the due date, same link and original.
        The note stays (it remembers which to-do it became), so nothing is lost."""
        from .agent import DATE_WORDS
        todos, notes = self._stores()[0], self._stores()[1]
        n = next((x for x in notes.items if x.get("id") == note_id), None)
        if not n:
            raise ValueError("That note no longer exists.")
        existing = next((t for t in todos.items if t["id"] == n.get("todo_id")), None)
        if existing:
            return {"title": existing["title"], "todo_id": existing["id"], "already": True}
        items = n.get("items") or {}
        title = (n.get("title") or f"{n['source']} — {n['subject']}").strip()
        due = next((d for d in (items.get("deadline"), items.get("event"), items.get("expiry"))
                    if isinstance(d, str) and DATE_WORDS.search(d)), None)
        text = n.get("text")
        about = text.strip().splitlines()[0][:200] if text and text.strip() else n.get("summary")
        details = {"note": text} if text is not None else dict(items)
        t = todos.add(title, due, n["source"], details, n.get("email_id", ""), n.get("ref"),
                      link=n.get("link"), link_label=n.get("link_label"),
                      about=about if about and about != title else None)
        n["todo_id"] = t["id"]
        notes.save()
        return {"title": title, "todo_id": t["id"], "already": False}

    # ------------------------------------------------------------------ clean-up of old items
    def cleanup_preview(self, days: int, keep_upcoming: bool) -> dict[str, Any]:
        todos, notes, _, _, originals = self._stores()
        p = cleanup.plan(todos, notes, originals, days, keep_upcoming)
        return {"days": days, "notes": len(p["notes"]), "todos": len(p["todos"]),
                "kept_upcoming": p["kept_upcoming"], "notes_total": p["notes_total"],
                "todos_total": p["todos_total"], "last": cleanup.last_batch(self.data_dir)}

    def cleanup_run(self, days: int, keep_upcoming: bool) -> dict[str, Any]:
        if self.status in ("connecting", "running"):
            raise ValueError("Finish or stop the current session first, then clean up.")
        todos, notes, _, _, originals = self._stores()
        r = cleanup.run(self.data_dir, todos, notes, originals, days, keep_upcoming)
        return {"removed_notes": r["removed_notes"], "removed_todos": r["removed_todos"]}

    def cleanup_undo(self) -> dict[str, int]:
        if self.status in ("connecting", "running"):
            raise ValueError("Finish or stop the current session first.")
        todos, notes = self._stores()[:2]
        return cleanup.undo(self.data_dir, todos, notes)

    def todo_reopen(self, todo_id: int) -> None:
        self._stores()[0].reopen(todo_id)

    def todo_delete(self, todo_id: int) -> None:
        self._stores()[0].delete(todo_id)

    def edit_note(self, note_id: int, text: str, title: str | None) -> None:
        if not self._stores()[1].edit(note_id, text, title):
            raise ValueError("That note no longer exists.")

    def delete_note(self, note_id: int) -> None:
        self._stores()[1].delete(note_id)

    def delete_rule(self, rule_id: str) -> None:
        self._stores()[2].delete(rule_id)

    def restore(self, ref: int) -> str:
        originals = self._stores()[4]
        email, entry = originals.load(ref), originals.entry(ref)
        if not email:
            raise ValueError(f"No email #{ref}.")
        if self.mode == "demo":
            mb = LocalMailbox(DEMO_DIR / "mailbox")
        else:
            if self.status in ("connecting", "running"):
                raise ValueError("Finish or stop the current session first, then put it back.")
            user = entry.get("account") or (self.accounts.items[0]["user"] if len(self.accounts.items) == 1 else None)
            acc = self.accounts.find(user) if user else None
            if not acc:
                raise ValueError("I don't know which mailbox this email came from.")
            mb = IMAPMailbox(acc["host"], acc["user"], self._password(acc["user"]))
        try:
            result = mb.restore(email)
        finally:
            mb.close()
        if result.startswith(("moved back", "already")):
            originals.update(ref, location="inbox")
        return result

    def set_mode(self, mode: str) -> None:
        if self.status not in ("connecting", "running") and mode in ("demo", "gmail"):
            self.mode = mode
            self.status, self.ui, self.agent, self.stats, self.error = "idle", None, None, {}, None


# --------------------------------------------------------------------------- #
def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # keep the terminal quiet
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, data: Any, code: int = 200) -> None:
            self._send(code, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _body(self) -> dict[str, Any]:
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}") if n else {}

        def do_GET(self):
            app.last_seen = time.monotonic()
            url = urlparse(self.path)
            parts = [p for p in url.path.split("/") if p]
            try:
                if not parts:
                    return self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
                if parts == ["api", "ping"]:   # cheap "is this Mail Agent?" check for a second launch
                    return self._json({"app": "mail-agent"})
                if parts == ["api", "state"]:
                    since = int(parse_qs(url.query).get("since", ["0"])[0])
                    return self._json(app.state(since))
                if parts == ["api", "panels"]:
                    return self._json(app.panels())
                if parts[:2] == ["api", "original"] and len(parts) == 3:
                    return self._json(app.original(int(parts[2])))
                if parts[:2] == ["api", "original"] and len(parts) == 4 and parts[3] == "view":
                    q = parse_qs(url.query)
                    images = q.get("images", ["0"])[0] == "1"
                    page = app.original_page(int(parts[2]), images, q.get("plain", ["0"])[0] == "1")
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Security-Policy", render.csp(images))
                    self.send_header("X-Content-Type-Options", "nosniff")
                    self.send_header("Referrer-Policy", "no-referrer")
                    self.send_header("Cache-Control", "no-store")
                    self.send_header("Content-Length", str(len(page)))
                    self.end_headers()
                    self.wfile.write(page)
                    return
                self._json({"error": "not found"}, 404)
            except Exception as exc:
                self._json({"error": str(exc)}, 400)

        def do_POST(self):
            app.last_seen = time.monotonic()
            # only accept requests from our own page (blocks other websites from poking the local server)
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).hostname not in ("127.0.0.1", "localhost"):
                return self._json({"error": "forbidden"}, HTTPStatus.FORBIDDEN)
            parts = [p for p in urlparse(self.path).path.split("/") if p]
            try:
                body = self._body()
                if parts == ["api", "start"]:
                    app.start(body)
                elif parts == ["api", "answer"]:
                    if not (app.ui and app.ui.answer(int(body["id"]), str(body["value"]))):
                        return self._json({"error": "That question is no longer open."}, 409)
                elif parts == ["api", "stop"]:
                    app.stop()
                elif parts == ["api", "quit"]:
                    self._json({"ok": True})
                    app.stop()
                    if app.request_quit:
                        app.request_quit()
                    return
                elif parts[:2] == ["api", "cleanup"] and len(parts) == 3:
                    days = max(1, min(int(body.get("days") or 30), 3650))
                    keep = bool(body.get("keep_upcoming", True))
                    if parts[2] == "preview":
                        return self._json({"ok": True, **app.cleanup_preview(days, keep)})
                    if parts[2] == "run":
                        return self._json({"ok": True, **app.cleanup_run(days, keep)})
                    if parts[2] == "undo":
                        return self._json({"ok": True, **app.cleanup_undo()})
                    return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
                elif parts == ["api", "accounts", "add"]:
                    return self._json({"ok": True, "account": app.add_account(
                        body.get("user", ""), body.get("password", ""), body.get("host", ""))})
                elif parts == ["api", "accounts", "remove"]:
                    app.remove_account(body.get("user", ""))
                elif parts == ["api", "apikey", "set"]:
                    app.set_api_key(body.get("key", ""))
                elif parts == ["api", "apikey", "remove"]:
                    app.remove_api_key()
                elif parts == ["api", "reader"]:
                    app.set_reader(body.get("reader", "builtin"), body.get("model"))
                elif parts == ["api", "local", "update"]:
                    app.start_pull(app.local_model(), update=True)
                elif parts == ["api", "local", "pull"]:
                    app.start_pull(body.get("model") or app.local_model())
                elif parts == ["api", "mode"]:
                    app.set_mode(body.get("mode", "demo"))
                elif parts[:2] == ["api", "todo"] and len(parts) == 4 and parts[3] == "done":
                    app.todo_done(int(parts[2]))
                elif parts[:2] == ["api", "todo"] and len(parts) == 4 and parts[3] == "undo":
                    app.todo_reopen(int(parts[2]))
                elif parts[:2] == ["api", "todo"] and len(parts) == 4 and parts[3] == "rewrite":
                    return self._json({"ok": True, **app.rewrite_todo(int(parts[2]))})
                elif parts[:2] == ["api", "note"] and len(parts) == 4 and parts[3] == "rewrite":
                    return self._json({"ok": True, **app.rewrite_note(int(parts[2]))})
                elif parts[:2] == ["api", "todo"] and len(parts) == 4 and parts[3] == "delete":
                    app.todo_delete(int(parts[2]))
                elif parts[:2] == ["api", "note"] and len(parts) == 4 and parts[3] == "todo":
                    return self._json({"ok": True, **app.note_to_todo(int(parts[2]))})
                elif parts[:2] == ["api", "note"] and len(parts) == 4 and parts[3] == "edit":
                    app.edit_note(int(parts[2]), body.get("text", ""), body.get("title"))
                elif parts[:2] == ["api", "note"] and len(parts) == 4 and parts[3] == "delete":
                    app.delete_note(int(parts[2]))
                elif parts[:2] == ["api", "rule"] and len(parts) == 4 and parts[3] == "delete":
                    app.delete_rule(parts[2])
                elif parts[:2] == ["api", "restore"] and len(parts) == 3:
                    return self._json({"ok": True, "result": app.restore(int(parts[2]))})
                else:
                    return self._json({"error": "not found"}, 404)
                self._json({"ok": True})
            except Exception as exc:
                self._json({"error": str(exc)}, 400)

    return Handler


def find_running(port: int = 8765, span: int = 20) -> str | None:
    """The address of a Mail Agent that is already running on this computer, if any."""
    import urllib.request
    for p in range(port, port + span):
        url = f"http://127.0.0.1:{p}/"
        try:
            with urllib.request.urlopen(url + "api/ping", timeout=2) as r:
                data = json.loads(r.read())
        except Exception:   # nothing there (refused at once), or another program
            continue
        if isinstance(data, dict) and data.get("app") == "mail-agent":   # it's us, not another program
            return url
    return None


def idle_for_too_long(app: "App", minutes: float, now: float | None = None) -> bool:
    """Quit-when-idle rule: no page has talked to us for a while, and no session is running."""
    now = time.monotonic() if now is None else now
    return app.status not in ("connecting", "running") and now - app.last_seen > minutes * 60


def serve(data_dir: Path, port: int = 8765, engine: str = "auto", model: str = DEFAULT_MODEL,
          open_browser: bool = True, quit_when_idle: float | None = None) -> None:
    running = find_running(port)
    if running:   # one copy at a time: two would write the same rules, notes and to-dos
        print(f"Mail Agent is already running at {running}")
        if open_browser:
            webbrowser.open(running)
        return
    app = App(data_dir, engine, model)
    server = None
    for p in range(port, port + 20):
        try:
            server = ThreadingHTTPServer(("127.0.0.1", p), make_handler(app))
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit(f"No free port between {port} and {port + 19}.")
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    app.request_quit = lambda: threading.Thread(target=server.shutdown, daemon=True).start()
    print(f"Mail Agent is running at {url}")
    print("Keep this window open while you use it. Quit from the page, or press Ctrl+C here.")
    if open_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    if quit_when_idle:
        def watch():
            while True:
                time.sleep(30)
                if idle_for_too_long(app, quit_when_idle):
                    print(f"No page open for {quit_when_idle:g} minutes — quitting.")
                    app.request_quit()
                    return
        threading.Thread(target=watch, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("Bye!")
        app.stop()
        server.server_close()
