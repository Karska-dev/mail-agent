"""The agent loop: analyze → match against learned rules → act or ask → learn."""
from __future__ import annotations

import json
import re
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

from .mailbox import Mailbox, unsubscribe
from .models import ACTION_TYPES, EXTRACT_FIELDS, TERMINAL_ACTIONS, Analysis, Email, Rule, describe_actions
from .rules import Match, RuleStore, copy_rule, evaluate, explain_rule, make_rule
from .ui import StopSession
from .stores import JsonList, Notes, Originals, ReadLog, Todos

UNSUBSCRIBE_NUDGE_AFTER = 3  # delete-only rule used this often on bulk mail -> offer to unsubscribe

# Typed shortcuts accepted whenever the agent asks what to do.
ONE_OFF_RE = re.compile(r"^\s*r\s*$|^\s*(?:just\s+)?read (?:it )?(?:and|&) archive(?: it)?[\s,.]*(?:no rule)?\.?\s*$"
                        r"|\bone[- ]?off\b|\bno rule\b|\bdon'?t (?:make|create|learn)"
                        r"(?: a)? rule", re.I)
VIEW_RE = re.compile(r"^\s*(?:v|view|show|original|full)\s*$|^\s*(?:please\s+)?(?:show|view|open|see)(?: me)?"
                     r" (?:the |this )?(?:full |whole |original |entire )*(?:email|original|letter|message|it)\s*[.!?]?\s*$",
                     re.I)
ONE_OFF_LABEL = "Just read it & archive — no rule"
VIEW_LABEL = "Show me the full original email"


DATE_WORDS = re.compile(r"\d|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|mon|tue|wed|thu|fri|sat|sun|"
                        r"today|tomorrow|tonight|next week|end of)", re.I)


def todo_title_and_due(analysis: Analysis, email: Email, override: str | None = None) -> tuple[str, str | None]:
    """A to-do that says what it's really about (not the email's button text) and a due date that is a date."""
    from .llm import looks_like_cta
    ex = analysis.extracted
    candidates = [override, analysis.task_title, *(ex.get("action_items") or [])]
    title = next((c.strip() for c in candidates if c and c.strip() and not looks_like_cta(c.strip())), None)
    if not title:
        title = analysis.note_title or f"{analysis.sender_label}: {email.subject}"
    due = next((d for d in (ex.get("deadline"), ex.get("event"), ex.get("expiry"))
                if isinstance(d, str) and DATE_WORDS.search(d)), None)
    return title, due


def _sort_date(e: Email):
    from email.utils import parsedate_to_datetime
    try:
        return parsedate_to_datetime(e.date).timestamp()
    except Exception:
        return float("inf")  # no date: treat as just arrived


class _SkipEmail(Exception):
    """The user chose 'skip for now' mid-workflow: leave the email untouched for next session."""


def order_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Show-to-read first, then extraction, then unsubscribe, then one terminal action (the last one named)."""
    show = [a for a in actions if a["type"] == "show"][:1]
    body = [a for a in actions if a["type"] in ("save_note", "add_todo")]
    unsub = [a for a in actions if a["type"] == "unsubscribe"][:1]
    terminal = [a for a in actions if a["type"] in TERMINAL_ACTIONS][-1:]
    return show + body + unsub + terminal


class EmailAgent:
    def __init__(self, data_dir: Path, mailbox: Mailbox, analyzer, parser, ui, min_confidence: float = 0.6,
                 interactive: bool = True, account: str | None = None):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.mailbox = mailbox
        self.analyzer = analyzer
        self.parser = parser
        self.ui = ui
        self.min_confidence = min_confidence
        self.interactive = interactive
        self.account = account  # which mailbox this session reads (for multi-account setups)
        self._last_analysis: Analysis | None = None
        self._prefetched: dict[str, Future] = {}
        self._pool: ThreadPoolExecutor | None = None
        self._upcoming: Email | None = None
        self.rules = RuleStore(self.data_dir / "rules.json")
        self.notes = Notes(self.data_dir / "notes.json")
        self.todos = Todos(self.data_dir / "todos.json")
        self.log = JsonList(self.data_dir / "activity_log.json")
        self.unsubscribed = JsonList(self.data_dir / "unsubscribed.json")
        self.read_log = ReadLog(self.data_dir / "read_log.json")
        self.originals = Originals(self.data_dir / "originals")
        state = self.data_dir / "processed.json"
        self._processed_path = state
        self.processed: set[str] = set(json.loads(state.read_text())) if state.exists() else set()
        self.auto_mode = False
        self.live_stats: dict[str, int] | None = None
        self._seen_this_session: set[str] = set()   # skipped/failed/practice emails: don't loop back to them

    # ------------------------------------------------------------------ #
    def run(self, limit: int = 30, days: int | None = 14, include_read: bool = False,
            newest_first: bool = False, watch_arrivals: bool = False) -> dict[str, int]:
        """One session: the newest `limit` unhandled emails from the last `days` days — unread ones,
        or everything in the inbox with include_read (for clearing out an inbox).
        The user can stop at any question ("q"); the rest stays in the inbox for next time.
        newest_first: go from the most recent email backwards (real mailboxes; the demo reads in date order).
        watch_arrivals: after each email, ask the server for mail that came in meanwhile and put it first."""
        self._repair_earlier_moves()
        self._busy("Checking your inbox…" if include_read else "Checking for new mail…")
        emails, total = self.mailbox.fetch_new(limit, days, exclude=self.processed, include_read=include_read)
        stats = {"auto": 0, "asked": 0, "one_off": 0, "skipped": 0, "left": 0}
        self.live_stats = stats   # the app reads this while the session runs
        window = f" from the last {days} days" if days else ""
        kind = "email" if include_read else "new email"
        if not emails:
            self.ui.info(f"No {kind}s left to go through{window} — inbox zero." if include_read
                         else f"Nothing new{window} — inbox zero.")
            return stats
        more = f"; this session covers the newest {len(emails)}" if total > len(emails) else ""
        where = " in your inbox (read and unread)" if include_read else ""
        self.ui.info(f"{total} {kind}{'s' if total != 1 else ''}{where}{window}{more}. "
                     "You can stop at any point and continue later.")
        stats["left"] = total - len(emails)
        queue: list[tuple[Email, bool]] = [(e, False) for e in (emails if newest_first else list(reversed(emails)))]
        seen = {e.id for e in emails}
        done = 0
        self._pool = ThreadPoolExecutor(max_workers=1) if self._slow_reader() else None
        try:
            return self._loop(queue, seen, done, stats, limit, include_read, watch_arrivals)
        finally:
            if self._pool:
                self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool, self._prefetched, self._upcoming = None, {}, None

    def run_auto(self, days: int | None = None, include_read: bool = False, batch: int = 20,
                 watch_arrivals: bool = True) -> dict[str, int]:
        """Auto mode: go through the whole inbox without asking anything.
        Emails your rules cover confidently are handled; anything that would need you (new kinds of email,
        unsure matches, suggestions) is left untouched — still unread — for your next normal session.
        Rules that say "show me first" can't show anything here, so those emails get a note and are archived.
        Mail is fetched in small batches, so a huge inbox doesn't have to be downloaded up front."""
        self.interactive, self.auto_mode = False, True
        self._repair_earlier_moves()
        stats = {"auto": 0, "asked": 0, "one_off": 0, "skipped": 0, "left": 0}
        self.live_stats = stats   # the app reads this while the session runs
        seen: set[str] = set()
        self._done = 0
        first = True
        self._pool = ThreadPoolExecutor(max_workers=1) if self._slow_reader() else None
        try:
            while True:
                if getattr(self.ui, "stop_requested", False):
                    stats["stopped"] = 1
                    self.ui.done("Stopped. Everything not handled yet stays in your inbox for next time.")
                    break
                self._busy("Checking your inbox…")
                emails, total = self._fetch_with_retry(batch, days, include_read)
                if first:
                    first = False
                    kind = "email" if include_read else "unread email"
                    if not emails:
                        self.ui.info(f"No {kind}s to go through — inbox zero.")
                        break
                    self.ui.info(f"Auto mode: {total} {kind}{'s' if total != 1 else ''} to go through. "
                                 "Your rules handle what they can; anything that needs you is skipped and stays "
                                 "unread for your next session. Press Stop any time.")
                if not emails:
                    break
                seen.update(e.id for e in emails)
                self._grand_total = self._done + total
                before = stats.get("stopped")
                self._loop([(e, False) for e in emails], seen, self._done, stats, 10 ** 9, include_read,
                           watch_arrivals, left_after=total - len(emails))
                if stats.get("stopped") and not before:
                    break
        finally:
            if self._pool:
                self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool, self._prefetched, self._upcoming = None, {}, None
            self._grand_total = None
        return stats

    def _fetch_with_retry(self, batch, days, include_read):
        exclude = self.processed | self._seen_this_session
        for attempt in range(3):
            try:
                return self.mailbox.fetch_new(batch, days, exclude=exclude, include_read=include_read)
            except Exception as exc:
                if attempt == 2 or not hasattr(self.mailbox, "reconnect"):
                    raise
                self.ui.notice(f"Lost the connection to the mail server ({exc}) — reconnecting…")
                time.sleep(3 * (attempt + 1))
                try:
                    self.mailbox.reconnect()
                except Exception:
                    pass

    def _loop(self, queue, seen, done, stats, limit, include_read, watch_arrivals, left_after: int = 0) -> dict[str, int]:
        while queue:
            email, fresh = queue.pop(0)
            self._upcoming = queue[0][0] if queue else None
            done += 1
            self._done = done
            if hasattr(self.ui, "set_email"):
                self.ui.set_email(email.id)
            try:
                if getattr(self.ui, "stop_requested", False):  # e.g. the Stop button in the app
                    raise StopSession
                ref = self.originals.save(email, account=self.account)  # keep a reviewable copy before anything is moved
                grand = getattr(self, "_grand_total", None)
                self.ui.header(done, max(grand, done + len(queue)) if grand else done + len(queue), email, ref,
                               fresh=fresh)
                outcome = self.process(email)
                self._seen_this_session.add(email.id)
            except (StopSession, KeyboardInterrupt, EOFError):
                remaining = len(queue) + 1 + left_after
                stats["left"] += remaining
                stats["stopped"] = 1
                self.ui.info("")
                self.ui.done(f"Stopped. {remaining} email{'s' if remaining != 1 else ''} left untouched in "
                             "your inbox — they'll come up next session.")
                break
            stats[outcome] += 1
            if watch_arrivals and hasattr(self.mailbox, "fetch_arrivals"):
                room = limit - done - len(queue)
                try:
                    arrived = [e for e in self.mailbox.fetch_arrivals(include_read=include_read)
                               if e.id not in seen and e.id not in self.processed]
                except Exception:
                    arrived = []  # a hiccup checking for new mail shouldn't end the session
                seen.update(e.id for e in arrived)
                arrived.sort(key=lambda e: _sort_date(e), reverse=True)
                take, extra = arrived[:max(room, 0)], arrived[max(room, 0):]
                stats["left"] += len(extra)
                if take:
                    queue = [(e, True) for e in take] + queue
                    self.ui.info(f"📨 {len(take)} new email{'s' if len(take) != 1 else ''} just arrived — "
                                 "showing next." + (f" ({len(extra)} more will wait for next session.)" if extra else ""))
                elif extra:
                    self.ui.info(f"📨 {len(extra)} new email{'s' if len(extra) != 1 else ''} arrived — "
                                 "this session is full, they'll come up next time.")
        return stats

    def _repair_earlier_moves(self) -> None:
        """Before replies were checked, a move Gmail refused could be reported as done. For those older
        entries (never 'verified'), look once whether the email is still in the inbox and redo the move."""
        if self.practice or not hasattr(self.mailbox, "find_in_inbox"):
            return
        todo = [e for e in self.originals.index
                if e.get("account") == self.account and e.get("location") in ("trash", "archive")
                and not e.get("verified") and "practice" not in (e.get("handled_by") or "")]
        if not todo:
            return
        self._busy(f"Double-checking {len(todo)} earlier moves…")
        fixed, failed = 0, 0
        for e in todo:
            try:
                uid = self.mailbox.find_in_inbox(e["email_id"])
                if uid:
                    stub = Email(id=e["email_id"], sender=e["from"], subject=e["subject"], body="", uid=uid)
                    (self.mailbox.delete if e["location"] == "trash" else self.mailbox.archive)(stub)
                    fixed += 1
                self.originals.update(e["ref"], verified=True)
            except Exception as exc:
                failed += 1
                last_error = exc
        if fixed:
            self.ui.done(f"Finished {fixed} earlier move{'s' if fixed != 1 else ''} that hadn't gone through "
                         "(they were still in your inbox).")
        if failed:
            self.ui.notice(f"{failed} earlier move{'s' if failed != 1 else ''} still couldn't be done: {last_error}")

    # ------------------------------------------------------------------ reading, fast
    def _slow_reader(self) -> bool:
        return getattr(self.analyzer, "name", "") in ("local", "claude")

    def _analyze(self, email: Email) -> Analysis:
        """Read an email, as cheaply as possible:
        1. already read in the background while you were answering the previous one → instant
        2. the built-in reader recognises it as matching one of your sender rules → instant, no AI needed
        3. otherwise ask the AI reader."""
        start = time.time()
        fut = self._prefetched.pop(email.id, None)
        if fut is not None:
            if not fut.done():
                self._busy(f"Reading this email{self._reader_name()}… (started while you were answering)")
            try:
                a = fut.result()
                a.seconds = round(time.time() - start, 1)
                return a
            except Exception:
                pass
        if self._slow_reader():
            quick = self._fast_path(email)
            if quick is not None:
                return quick
        self._busy(f"Reading this email{self._reader_name()}…")
        a = self.analyzer.analyze(email)
        a.seconds = round(time.time() - start, 1)
        return a

    def _fast_path(self, email: Email) -> Analysis | None:
        from .analyzer import HeuristicAnalyzer
        h = HeuristicAnalyzer().analyze(email)
        m = evaluate(email, h, self.rules.rules, self.min_confidence)
        if (m.kind == "auto" and m.rule.scope == "sender" and h.confidence >= 0.7 and h.risk == "none"
                and h.importance != "high"):
            h.engine = "built-in (matches your rule — no AI needed)"
            return h
        return None

    def _prefetch_next(self) -> None:
        """While you look at this email, read the next one in the background."""
        if not self._slow_reader() or self._pool is None or not self._upcoming:
            return
        nxt = self._upcoming
        if nxt.id in self._prefetched or (self._fast_path(nxt) is not None):
            return
        self._prefetched[nxt.id] = self._pool.submit(self._timed_read, nxt)

    def _timed_read(self, email: Email) -> Analysis:
        start = time.time()
        a = self.analyzer.analyze(email)
        a.seconds = round(time.time() - start, 1)
        return a

    def _busy(self, text: str) -> None:
        """Tell the UI what we're doing during slow steps (the app shows a spinner)."""
        if hasattr(self.ui, "busy"):
            self.ui.busy(text)

    def _reader_name(self) -> str:
        return {"local": " with local AI", "claude": " with Claude"}.get(getattr(self.analyzer, "name", ""), "")

    def process(self, email: Email) -> str:
        try:
            return self._process(email)
        except _SkipEmail:
            self.ui.info("   OK, leaving it for now — it'll come up next session.")
            return "skipped"

    def _process(self, email: Email) -> str:
        analysis = self._analyze(email)
        self._last_analysis = analysis
        self._prefetch_next()
        self.ui.show_analysis(analysis)
        match = evaluate(email, analysis, self.rules.rules, self.min_confidence)

        if match.kind == "auto" and not self.interactive and any(a["type"] == "show" for a in match.rule.actions):
            if not self.auto_mode:
                self.ui.notice(f"Rule \"{match.rule.name}\" wants you to read this one first — left for your next session.")
                return "skipped"
            # auto mode: nobody is here to read it, so keep it in notes (to read later) and archive it
            plan = [a for a in match.rule.actions if a["type"] not in ("show",) + tuple(TERMINAL_ACTIONS)]
            if not any(a["type"] == "save_note" for a in plan):
                plan.append({"type": "save_note"})
            plan.append({"type": "archive"})
            self.ui.done(f"Rule {match.rule.id} \"{match.rule.name}\" says to read it first — auto mode saves it "
                         "to notes and archives it instead (the original is in History).")
            self._execute(email, analysis, plan, source=f"rule {match.rule.id} (auto mode: noted & archived)")
            self.rules.record_use(match.rule)
            return "auto"
        if match.kind == "auto":
            self.ui.done(f"Rule {match.rule.id} \"{match.rule.name}\" applies — doing it automatically.")
            self._apply_rule(email, analysis, match.rule)
            self._maybe_offer_unsubscribe(email, analysis, match.rule)
            return "auto"
        if not self.interactive:
            why = {"confirm": "a rule might fit but I'm not sure", "suggest": "a similar workflow might fit",
                   "partial": "it's a bit different from your rule"}.get(match.kind, "new kind of email")
            self.ui.notice(f"Needs you ({why}) — skipped, it stays unread for your next session.")
            return "skipped"
        if match.kind == "confirm":
            return self._confirm(email, analysis, match)
        if match.kind == "suggest":
            return self._suggest(email, analysis, match)
        if match.kind == "partial":
            self.ui.notice(match.reason)
        else:
            self.ui.notice("I haven't seen an email like this before.")
        return self._ask(email, analysis)

    # ------------------------------------------------------------------ #
    def _confirm(self, email: Email, analysis: Analysis, match: Match) -> str:
        self.ui.notice(match.reason)
        options = [f"Yes — apply \"{match.rule.name}\" ({describe_actions(match.rule.actions)})"]
        options += [f"Apply \"{r.name}\" instead ({describe_actions(r.actions)})" for r in match.others]
        options += ["No — I'll tell you what to do", "Skip for now"]
        pick = self._choose(email, "Should I go ahead?", options)
        rules = [match.rule] + match.others
        if pick == "one_off":
            return self._one_off(email, analysis)
        if pick < len(rules):
            self._apply_rule(email, analysis, rules[pick])
            return "auto"
        if pick == len(rules):
            return self._ask(email, analysis)
        return "skipped"

    def _suggest(self, email: Email, analysis: Analysis, match: Match) -> str:
        r = match.rule
        self.ui.notice(f"{match.reason}\n     Workflow: {describe_actions(r.actions)}")
        label = analysis.sender_label
        pick = self._choose(email, "Reuse it?", [
            f"Yes, and remember it for {label} too (new {label}-specific rule)",
            f"Yes, and use it for any sender from now on",
            "Yes, just this once",
            "No — I'll tell you what to do",
        ])
        if pick == "one_off":
            return self._one_off(email, analysis)
        if pick == 3:
            return self._ask(email, analysis)
        if pick in (0, 1):
            scope = "sender" if pick == 0 else "any"
            new = self.rules.add(copy_rule(r, scope=scope, email=email, analysis=analysis))
            self.rules.record_use(new)
            self.ui.done(f"Learned rule {new.id}: {new.name}")
        else:
            self.rules.record_use(r)
        self._execute(email, analysis, r.actions, source=f"workflow of {r.id}")
        return "asked"

    def _ask(self, email: Email, analysis: Analysis) -> str:
        while True:
            text = self.ui.ask_text("What should I do with it? (e.g. \"save the code to notes and delete\")")
            if text.strip().lower() == "s":
                text = "skip"
            if VIEW_RE.search(text):
                self._show_original(email)
                continue
            if ONE_OFF_RE.search(text):
                return self._one_off(email, analysis)
            self._busy(f"Understanding your answer{self._reader_name()}…")
            actions = self.parser.parse(text, analysis)
            if actions == []:
                self.ui.info("   OK, leaving it for now — I'll ask again next time.")
                return "skipped"
            if actions is None:
                self.ui.notice("Sorry, I didn't catch that. Try words like notes, to-do, archive, delete, "
                               "unsubscribe, keep — or 'skip'.")
                continue
            actions = order_actions(actions)
            pick = self.ui.choose(f"I'll: {describe_actions(actions)}. Remember this?", [
                f"Yes — for {analysis.sender_label} emails like this",
                "Yes — for similar emails from any sender",
                "Just this once",
                "That's not what I meant (rephrase)",
            ])
            if pick == 3:
                continue
            if pick in (0, 1):
                rule = self.rules.add(make_rule(email, analysis, actions, "sender" if pick == 0 else "any", text))
                self.rules.record_use(rule)
                self.ui.done(f"Learned rule {rule.id}: {rule.name}")
            self._execute(email, analysis, actions, source="your instruction")
            return "asked"

    # ------------------------------------------------------------------ #
    def _choose(self, email: Email, prompt: str, options: list[str]):
        """A menu that always also offers 'view original' and 'read & archive, no rule'.
        Returns the index into `options`, or "one_off"."""
        full = options + [ONE_OFF_LABEL, VIEW_LABEL]
        while True:
            pick = self.ui.choose(prompt, full)
            if pick == len(options) + 1:
                self._show_original(email)
                continue
            return "one_off" if pick == len(options) else pick

    def _show_original(self, email: Email) -> None:
        self.ui.show_full(email, self.originals.ref_for(email.id))

    def _one_off(self, email: Email, analysis: Analysis) -> str:
        """Read it for the user (digest entry), mark read, archive. Deliberately learns nothing."""
        ref = self.originals.ref_for(email.id)
        facts = {k: v for k, v in analysis.values().items() if k not in ("links", "dates", "amounts")}
        if self.practice:
            self.ui.done("practice: would save a summary to your read log, mark as read and archive (no rule)")
            self._finish(email, analysis, "one-off (practice)", ["read", "archive"], [], None)
            return "one_off"
        self.read_log.add(ref, analysis.sender_label, email.subject, email.date, analysis.summary, facts)
        results = ["summary saved to your read log"]
        try:
            results += [self.mailbox.mark_read(email), self.mailbox.archive(email)]
        except Exception as exc:
            for r in results:
                self.ui.done(r)
            self.ui.notice(f"Couldn't archive it: {exc}. It stays in your inbox and will come up again.")
            self._finish(email, analysis, "one-off (failed)", ["read", "archive"], results, None, failed=True)
            return "skipped"
        for r in results:
            self.ui.done(r)
        self.ui.done("no rule created — similar emails will still be asked about")
        self._finish(email, analysis, "one-off (read & archive)", ["read", "archive"], results, "archive",
                     verified=True)
        return "one_off"

    def _read_first(self, email: Email, actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The 'show' step: put the whole email in front of the user, wait until they've read it,
        then let them follow the plan or change their mind for this one email (the rule stays)."""
        plan = [a for a in order_actions(actions) if a["type"] != "show"]
        ref = self.originals.ref_for(email.id)
        (getattr(self.ui, "show_reading", None) or self.ui.show_full)(email, ref)
        terminal = next((a["type"] for a in plan if a["type"] in TERMINAL_ACTIONS), None)
        options: list[tuple[str, Any]] = [(f"Done — {describe_actions(plan) if plan else 'move on'}, as planned", plan)]
        if terminal != "archive":
            options.append(("Archive it instead", [{"type": "archive"}]))
        if terminal != "delete":
            options.append(("Delete it instead", [{"type": "delete"}]))
        if terminal != "keep":
            options.append(("Keep it in my inbox", [{"type": "keep"}]))
        options += [("Something else — I'll tell you", "other"),
                    ("Skip for now (show it again next session)", "skip")]
        pick = options[self.ui.choose("Take your time. What now that you've read it?", [o[0] for o in options])][1]
        if pick == "skip":
            raise _SkipEmail
        if pick != "other":
            if pick is not plan:
                self.ui.done("Changed for this email only — your rule stays the same.")
            return pick
        while True:
            text = self.ui.ask_text("What should I do with it instead? (just this once)")
            self._busy(f"Understanding your answer{self._reader_name()}…")
            new = self.parser.parse(text, self._last_analysis) if self._last_analysis else None
            if new == []:
                raise _SkipEmail
            if new:
                new = [a for a in order_actions(new) if a["type"] != "show"]
                m = re.search(r"(?:to-?\s?do|remind(?:er)?(?: me)?)\s+(?:to|for|about|:)?\s*(.+?)"
                              r"(?:,|\.|\s+(?:and|then)\s+(?:archive|delete|keep|leave)|$)", text, re.I)
                for a in new:  # one-time plan: use the user's own words as the to-do title
                    if a["type"] == "add_todo" and m and len(m.group(1)) > 3:
                        a["title"] = m.group(1).strip()[:1].upper() + m.group(1).strip()[1:]
                if new:
                    self.ui.done("Changed for this email only — your rule stays the same.")
                    return new
            self.ui.notice("Sorry, I didn't catch that. Try: archive, delete, keep, add a to-do, save to notes…")

    def _apply_rule(self, email: Email, analysis: Analysis, rule: Rule) -> None:
        self._execute(email, analysis, rule.actions, source=f"rule {rule.id}")
        self.rules.record_use(rule)

    def _maybe_offer_unsubscribe(self, email: Email, analysis: Analysis, rule: Rule) -> None:
        types = {a["type"] for a in rule.actions}
        if (self.interactive and types == {"delete"} and analysis.has("is_bulk") and not rule.unsubscribe_offered
                and rule.times_applied >= UNSUBSCRIBE_NUDGE_AFTER):
            rule.unsubscribe_offered = True
            self.rules.save()
            pick = self.ui.choose(
                f"You've deleted {rule.times_applied} of these from {analysis.sender_label} without reading. "
                "Unsubscribe?", ["Yes, unsubscribe and keep deleting", "No, keep getting them"])
            if pick == 0:
                self._execute(email, analysis, [{"type": "unsubscribe"}], source="unsubscribe nudge",
                              already_filed=True)
                rule.actions = [{"type": "unsubscribe"}] + rule.actions
                self.rules.save()

    @property
    def practice(self) -> bool:
        """Practice mode (dry run): learn rules and show what would happen, but change nothing —
        no notes/to-dos written, no mail moved, and the email is NOT marked handled, so the first
        real run will process it (using whatever rules you taught it here)."""
        return bool(getattr(self.mailbox, "dry_run", False))

    def _execute(self, email: Email, analysis: Analysis, actions: list[dict[str, Any]], source: str,
                 already_filed: bool = False) -> None:
        ref = self.originals.ref_for(email.id)
        results = []
        location = None
        failed = ""
        if any(a["type"] == "show" for a in actions):
            actions = self._read_first(email, actions)
        self._busy("Saving and filing…")
        if self.practice:
            for action in order_actions(actions):
                if action["type"] in TERMINAL_ACTIONS and already_filed:
                    continue
                results.append("practice: would " + describe_actions([action]))
            for r in results:
                self.ui.done(r)
            self._finish(email, analysis, source + " (practice)", [a["type"] for a in actions], results, None)
            return
        for action in order_actions(actions):
            kind = action["type"]
            if kind == "save_note":
                items = analysis.values(action.get("fields") or None)
                items = {k: v for k, v in items.items() if k not in ("links", "amounts")} or items
                self.notes.add(analysis.sender_label, email.subject, email.id, analysis.category, items,
                               analysis.summary, ref, title=analysis.note_title or None,
                               link=analysis.key_link or None, link_label=analysis.key_link_label or None)
                results.append("saved to notes" + (f" ({', '.join(EXTRACT_FIELDS.get(k, k) for k in items)})"
                                                   if items else ""))
            elif kind == "add_todo":
                title, due = todo_title_and_due(analysis, email, action.get("title"))
                details = analysis.values(action.get("fields") or None)
                self.todos.add(title, due, analysis.sender_label, details, email.id, ref,
                               link=analysis.key_link or None, link_label=analysis.key_link_label or None,
                               about=analysis.summary)
                results.append(f"to-do added: \"{title}\"" + (f" (due {due})" if due else ""))
            elif kind == "unsubscribe":
                ok, msg, link = unsubscribe(email, self.mailbox.simulated)
                self.unsubscribed.append({"sender": email.sender, "domain": email.sender_domain,
                                          "done": ok, "detail": msg, "link": link,
                                          "at": datetime.now().isoformat(timespec="seconds")})
                if link:
                    self.todos.add(f"Unsubscribe from {analysis.sender_label}", None, analysis.sender_label,
                                   {"links": [link]}, email.id, ref)
                results.append(msg)
            elif kind in TERMINAL_ACTIONS and not already_filed:
                try:
                    results.append(getattr(self.mailbox, kind)(email))
                    location = {"archive": "archive", "delete": "trash", "keep": "inbox"}[kind]
                except Exception as exc:
                    failed = f"Couldn't {ACTION_TYPES.get(kind, kind)} it: {exc}. It stays in your inbox and will come up again."
        for r in results:
            self.ui.done(r)
        if failed:
            self.ui.notice(failed)
        self._finish(email, analysis, source, [a["type"] for a in actions], results + ([failed] if failed else []),
                     location, failed=bool(failed), verified=not failed)

    def _finish(self, email: Email, analysis: Analysis, source: str, actions: list[str], results: list[str],
                location: str | None, failed: bool = False, verified: bool = False) -> None:
        ref = self.originals.ref_for(email.id)
        self.log.append({"at": datetime.now().isoformat(timespec="seconds"), "ref": ref, "email_id": email.id,
                         "from": email.sender, "subject": email.subject, "category": analysis.category,
                         "source": source, "actions": actions, "results": results})
        update: dict[str, Any] = {"handled_by": source + (" — failed" if failed else "")}
        if location:
            update["location"] = location
        if verified and not self.practice:
            update["verified"] = True  # the server confirmed the move
        self.originals.update(ref, **update)
        if self.practice or failed:
            return  # stays "new": it will come up again next session
        self.processed.add(email.id)
        self._processed_path.write_text(json.dumps(sorted(self.processed)))

    # ------------------------------------------------------------------ #
    def describe_rules(self) -> str:
        if not self.rules.rules:
            return "No rules learned yet."
        return "\n\n".join(explain_rule(r) for r in self.rules.rules)
