"""End-to-end behaviour tests (offline engine, local mailbox)."""
import json
from pathlib import Path

import pytest

from email_agent.agent import EmailAgent
from email_agent.analyzer import HeuristicAnalyzer
from email_agent.demo import DEMO_EMAILS
from email_agent.instructions import HeuristicInstructionParser
from email_agent.mailbox import LocalMailbox
from email_agent.models import Email
from email_agent.rules import evaluate
from email_agent.ui import ScriptedUI

E = {e.id: e for e in DEMO_EMAILS}
UNSUB = {"List-Unsubscribe": "<https://x.example/unsub>"}


def make_agent(tmp: Path, emails, answers, analyzer=None):
    mb = LocalMailbox(tmp / "mailbox")
    mb.load_inbox(emails)
    return EmailAgent(tmp, mb, analyzer or HeuristicAnalyzer(), HeuristicInstructionParser(), ScriptedUI(answers)), mb


def coupon_mail(i, sender, code):
    return Email(i, sender, "Offer", f"Get 20% off with code {code}. Valid until October 30, 2026, online only.",
                 headers=UNSUB)


def test_learns_rule_then_applies_it_automatically(tmp_path):
    agent, mb = make_agent(tmp_path, [E["d01"], E["d02"]], {
        "d01": ["Save the coupon code and its conditions to my notes, then delete the email.", "1"]})
    stats = agent.run(days=None)
    assert stats == {"auto": 1, "asked": 1, "one_off": 0, "skipped": 0, "left": 0}
    assert mb.counts() == {"inbox": 0, "archive": 0, "trash": 2}
    codes = [n["items"]["coupon_codes"] for n in agent.notes.items]
    assert codes == [["FALL20OFF"], ["BDAY10NW"]]
    assert "conditions" in agent.notes.items[1]["items"]


def test_rules_persist_across_sessions(tmp_path):
    agent, _ = make_agent(tmp_path, [E["d01"]], {"d01": ["save the code to notes and delete", "1"]})
    agent.run(days=None)
    agent2, _ = make_agent(tmp_path, [E["d02"]], {})  # no answers: must not ask
    assert agent2.run(days=None)["auto"] == 1


def test_same_sender_without_coupon_is_partial_and_asks(tmp_path):
    agent, _ = make_agent(tmp_path, [E["d01"], E["d03"]], {
        "d01": ["save the code to notes and delete", "1"], "d03": ["archive", "1"]})
    agent.run(days=None)
    a = HeuristicAnalyzer().analyze(E["d03"])
    assert evaluate(E["d03"], a, agent.rules.rules[:1]).kind == "partial"
    # the ad rule must not swallow a future coupon mail, and vice-versa
    c = HeuristicAnalyzer().analyze(E["d02"])
    m = evaluate(E["d02"], c, agent.rules.rules)
    assert m.kind == "auto" and m.rule.id == "r1"


def test_other_retailer_gets_suggestion_and_can_generalise(tmp_path):
    emails = [E["d01"], E["d04"], coupon_mail("x1", "Trailhead Gear <n@trailhead.example>", "TRAIL25")]
    agent, _ = make_agent(tmp_path, emails, {
        "d01": ["save the code to notes and delete", "1"],
        "d04": ["2"],   # reuse for any sender
    })
    stats = agent.run(days=None)
    assert stats["auto"] == 1  # Trailhead Gear handled by the generalised rule
    assert [r.scope for r in agent.rules.rules] == ["sender", "any"]


def test_retailer_specific_copy(tmp_path):
    agent, _ = make_agent(tmp_path, [E["d01"], E["d04"]], {
        "d01": ["save the code to notes and delete", "1"], "d04": ["1"]})
    agent.run(days=None)
    assert agent.rules.rules[1].sender_domain == "fernway.example"


def test_low_confidence_asks_for_confirmation(tmp_path):
    class Unsure(HeuristicAnalyzer):
        def analyze(self, email):
            a = super().analyze(email)
            a.confidence = 0.4 if email.id == "d02" else a.confidence
            return a

    agent, mb = make_agent(tmp_path, [E["d01"], E["d02"]], {
        "d01": ["save the code to notes and delete", "1"], "d02": ["3"]}, analyzer=Unsure())  # 3 = skip
    stats = agent.run(days=None)
    assert stats["skipped"] == 1 and mb.counts()["inbox"] == 1


def test_bill_rule_carries_across_providers(tmp_path):
    agent, _ = make_agent(tmp_path, [E["d08"], E["d09"]], {
        "d08": ["Add a todo to pay it by the due date, save the account number and amount to my notes, "
                "and archive the email.", "2"]})
    assert agent.run(days=None)["auto"] == 1
    from email_agent.demo import on
    assert [t["due"] for t in agent.todos.items] == [on(8), on(14)]


def test_unsubscribe_nudge_after_repeated_deletes(tmp_path):
    mails = [Email(f"n{i}", "Spammy <s@spammy.example>", f"Deal {i}", "Huge sale! Shop now, limited time deal.",
                   headers=UNSUB) for i in range(4)]
    agent, _ = make_agent(tmp_path, mails, {"n0": ["delete", "1"], "n2": ["1"]})
    agent.run(days=None)
    assert agent.rules.rules[0].actions[0]["type"] == "unsubscribe"
    assert agent.unsubscribed.items


@pytest.mark.parametrize("text,expected", [
    ("Save the coupon code and its conditions to my notes, then delete the email.", ["save_note", "delete"]),
    ("Unsubscribe and delete it.", ["unsubscribe", "delete"]),
    ("don't delete it, just archive", ["archive"]),
    ("add a reminder to pay and archive", ["add_todo", "archive"]),
    ("skip", []),
])
def test_instruction_parser(text, expected):
    a = HeuristicAnalyzer().analyze(E["d01"])
    got = HeuristicInstructionParser().parse(text, a)
    assert [x["type"] for x in got] == expected


# --------------------------------------------------------------------------- #
# One-off "read & archive" and reviewing originals
# --------------------------------------------------------------------------- #
def test_one_off_read_and_archive_learns_nothing(tmp_path):
    twin = Email("d12b", E["d12"].sender, "Another exemption letter", E["d12"].body)
    agent, mb = make_agent(tmp_path, [E["d12"], twin], {"d12": ["v", "r"], "d12b": ["skip"]})
    stats = agent.run(days=None)
    assert stats["one_off"] == 1 and stats["skipped"] == 1   # the twin is asked about again
    assert agent.rules.rules == []
    assert mb.counts()["archive"] == 1
    assert agent.read_log.items[0]["ref"] == 1
    assert agent.originals.entry(1)["location"] == "archive"


def test_one_off_is_offered_in_suggestion_menu(tmp_path):
    agent, _ = make_agent(tmp_path, [E["d01"], E["d04"]], {
        "d01": ["save the code to notes and delete", "1"],
        "d04": ["6", "5"],   # 6 = show original (then menu again), 5 = read & archive, no rule
    })
    stats = agent.run(days=None)
    assert stats["one_off"] == 1 and len(agent.rules.rules) == 1


def test_originals_kept_and_restorable(tmp_path):
    agent, mb = make_agent(tmp_path, [E["d01"]], {"d01": ["save the code to notes and delete", "3"]})
    agent.run(days=None)
    ref = agent.originals.ref_for("d01")
    assert agent.originals.load(ref).body == E["d01"].body
    assert agent.notes.items[0]["ref"] == ref
    assert mb.restore(agent.originals.load(ref)).startswith("moved back")
    assert mb.counts()["inbox"] == 1


# --------------------------------------------------------------------------- #
# Session window and stopping early
# --------------------------------------------------------------------------- #
def test_session_limit_and_age_window(tmp_path):
    from datetime import date, timedelta
    today = date.today()
    mails = [Email(f"m{i}", "Friend <f@example.com>", f"Hi {i}", "Just saying hi, talk soon.",
                   date=(today - timedelta(days=d)).isoformat()) for i, d in enumerate([1, 2, 3, 20, 40])]
    agent, mb = make_agent(tmp_path, mails, {m.id: ["leave it in my inbox", "3"] for m in mails})
    emails, total = mb.fetch_new(limit=2, days=14)
    assert total == 3 and [e.id for e in emails] == ["m0", "m1"]      # newest first, old mail excluded
    stats = agent.run(limit=2, days=14)
    assert stats["asked"] == 2 and stats["left"] == 1


def test_stop_for_now_leaves_the_rest_untouched(tmp_path):
    agent, mb = make_agent(tmp_path, [E["d01"], E["d02"], E["d03"]], {
        "d01": ["save the code to notes and delete", "1"], "d03": ["q"]})
    stats = agent.run(days=None)
    assert stats["stopped"] and stats["left"] == 1 and mb.counts()["inbox"] == 1
    # next session picks up exactly where we stopped
    agent2 = EmailAgent(tmp_path, mb, HeuristicAnalyzer(), HeuristicInstructionParser(),
                        ScriptedUI({"d03": ["archive", "3"]}))
    assert agent2.run(days=None)["asked"] == 1 and mb.counts()["inbox"] == 0


def test_practice_mode_learns_but_changes_nothing(tmp_path):
    agent, mb = make_agent(tmp_path, [E["d01"], E["d02"]], {
        "d01": ["save the code to notes and delete", "1"]})
    mb.dry_run = True
    assert agent.run(days=None)["auto"] == 1           # the rule is learned and used...
    assert mb.counts()["inbox"] == 2                   # ...but nothing moved
    assert not agent.notes.items and not agent.processed
    mb.dry_run = False                                  # first real run: both handled by the rule
    agent2 = EmailAgent(tmp_path, mb, HeuristicAnalyzer(), HeuristicInstructionParser(), ScriptedUI({}))
    assert agent2.run(days=None)["auto"] == 2 and mb.counts()["trash"] == 2


# --------------------------------------------------------------------------- #
# "Make me read it, then delete" workflows
# --------------------------------------------------------------------------- #
SEC = [Email(f"sec{i}", s, "Security alert", "New sign-in to your account from a Mac. If this wasn't you, "
             "reset your password.") for i, s in enumerate(["Google <no-reply@accounts.google.com>",
                                                           "Apple <noreply@id.apple.com>"])]
SEC_TEXT = "all security related emails I want to read, view and then delete. just make me read it and then delete"


def test_read_first_rule_for_any_sender(tmp_path):
    agent, mb = make_agent(tmp_path, SEC, {
        "sec0": [SEC_TEXT, "2", "1"],   # teach: any sender · then "Done reading — now delete"
        "sec1": ["1"],                  # rule applies automatically, still shows it: "Done reading"
    })
    stats = agent.run(days=None)
    assert stats["asked"] == 1 and stats["auto"] == 1
    assert [a["type"] for a in agent.rules.rules[0].actions] == ["show", "delete"]
    assert agent.rules.rules[0].scope == "any" and mb.counts()["trash"] == 2


def test_read_first_skip_keeps_rule_and_email(tmp_path):
    agent, mb = make_agent(tmp_path, SEC[:1], {"sec0": [SEC_TEXT, "2", "5"]})  # 5 = skip for now
    stats = agent.run(days=None)
    assert stats["skipped"] == 1 and len(agent.rules.rules) == 1 and mb.counts()["inbox"] == 1
    assert "sec0" not in agent.processed


def test_unattended_run_leaves_read_first_mail(tmp_path):
    agent, mb = make_agent(tmp_path, SEC, {"sec0": [SEC_TEXT, "2", "1"]})
    mb.load_inbox(SEC[:1])
    agent.run(days=None)
    mb.load_inbox(SEC[1:])
    agent.interactive = False
    assert agent.run(days=None)["skipped"] == 1 and mb.counts()["inbox"] == 1


def test_ai_saying_skip_is_not_trusted():
    from email_agent.llm import trust_but_verify
    a = HeuristicAnalyzer().analyze(SEC[0])
    got = trust_but_verify([], SEC_TEXT, a, HeuristicInstructionParser())
    assert [x["type"] for x in got] == ["show", "delete"]
    assert trust_but_verify([], "skip", a, HeuristicInstructionParser()) == []


def test_change_mind_after_reading(tmp_path):
    agent, mb = make_agent(tmp_path, SEC, {
        "sec0": [SEC_TEXT, "2", "2"],                                   # read it -> archive instead
        "sec1": ["4", "add a to-do to check my account and archive it"],  # read it -> something else
    })
    stats = agent.run(days=None)
    assert stats == {"auto": 1, "asked": 1, "one_off": 0, "skipped": 0, "left": 0}
    assert mb.counts() == {"inbox": 0, "archive": 2, "trash": 0}
    assert [t["title"] for t in agent.todos.items] == ["Check my account"]
    assert [a["type"] for a in agent.rules.rules[0].actions] == ["show", "delete"]   # rule unchanged


# --------------------------------------------------------------------------- #
# Importance and phishing warnings
# --------------------------------------------------------------------------- #
def test_phishing_and_importance_flags():
    H = HeuristicAnalyzer()
    fake = H.analyze(Email("p1", "PayPal Service <support@paypa1-secure.co>", "Your account has been suspended",
                           "Verify your account within 24 hours: http://bit.ly/x or it will be closed."))
    lookalike = H.analyze(Email("p2", "Chase <alerts@chase-verify.net>", "Action required",
                                "Click here to update your payment details: https://chase-verify.net/login"))
    real = H.analyze(Email("p3", "Google <no-reply@accounts.google.com>", "Security alert",
                           "New sign-in to your Google Account on a Mac. Check activity at https://myaccount.google.com"))
    assert fake.risk == "high" and lookalike.risk == "high" and fake.importance == "high"
    assert real.risk == "none" and real.category == "security" and real.importance == "high"
    assert all(H.analyze(e).risk == "none" for e in DEMO_EMAILS)          # no false alarms on normal mail
    assert H.analyze(E["d01"]).importance == "low" and H.analyze(E["d08"]).importance == "high"


def test_ai_cannot_hide_warning_signs():
    from email_agent.llm import analysis_from
    e = Email("p1", "PayPal Service <support@paypa1-secure.co>", "Your account has been suspended",
              "Verify your account within 24 hours: http://bit.ly/x")
    a = analysis_from({"category": "security", "summary": "x", "sender_label": "PayPal", "features": [],
                       "confidence": 0.9, "importance": "low", "phishing_risk": "none", "phishing_signs": [],
                       "extracted": {}}, e, "local")
    assert a.risk == "high" and a.importance == "high" and a.risk_signs


def test_notes_edit_and_delete(tmp_path):
    from email_agent.stores import Notes
    n = Notes(tmp_path / "notes.json")
    a = n.add("Northwind", "Sale", "e1", "promotion", {"coupon_codes": ["FALL20OFF"]}, "20% off")
    b = n.add("Fernway", "Deal", "e2", "promotion", {"coupon_codes": ["FERN15"]}, "15% off")
    assert n.as_text(a) == "Coupon code: FALL20OFF"
    assert n.edit(a["id"], "Code FALL20OFF — used it already", "Northwind fall code")
    assert n.delete(b["id"]) and len(n.items) == 1
    again = Notes(tmp_path / "notes.json")                       # persisted
    assert again.items[0]["text"].startswith("Code FALL20OFF") and again.items[0]["title"] == "Northwind fall code"
    assert "Northwind fall code" in (tmp_path / "notes.md").read_text()
    new = again.add("X", "Y", "e3", "other", {}, "s")
    assert len({x["id"] for x in again.items}) == 2 and new["id"] != again.items[0]["id"]


# --------------------------------------------------------------------------- #
# Real-server safety: refused moves are never reported as done
# --------------------------------------------------------------------------- #
class FakeGmail:
    """Minimal Gmail-like IMAP server: localized Trash name, labels, MOVE."""
    TRASH = "[Gmail]/&BBoEPgRIBDgEOg-"

    def __init__(self, emails):
        self.box = {str(i + 1): {"mid": e.id, "labels": {"\\Inbox"}, "raw": (
            f"From: {e.sender}\r\nSubject: {e.subject}\r\nMessage-ID: {e.id}\r\nDate: Sat, 26 Sep 2026 10:00:00 -0400\r\n"
            f"\r\n{e.body}\r\n").encode()} for i, e in enumerate(emails)}
        self.capabilities = ("IMAP4REV1", "X-GM-EXT-1", "MOVE")

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "INBOX"', b'(\\All \\HasNoChildren) "/" "[Gmail]/All"',
                      f'(\\HasNoChildren \\Trash) "/" "{self.TRASH}"'.encode()]

    def inbox(self):
        return [u for u, m in self.box.items() if "\\Inbox" in m["labels"]]

    def uid(self, cmd, *a):
        if cmd == "SEARCH":
            if a[1] == "UID":
                lo = int(a[2].split(":")[0])
                return "OK", [" ".join(u for u in self.inbox() if int(u) >= lo).encode()]
            if a[1] == "HEADER":
                return "OK", [" ".join(u for u in self.inbox() if self.box[u]["mid"] == a[3].strip('"')).encode()]
            return "OK", [" ".join(self.inbox()).encode()]
        if cmd == "FETCH":
            uids = a[0].decode().split(",") if isinstance(a[0], bytes) else [a[0]]
            out = []
            for u in uids:
                u = u.decode() if isinstance(u, bytes) else u
                out += [(f"1 (UID {u} BODY[] {{1}}".encode(), self.box[u]["raw"]), b")"]
            return "OK", out
        if cmd == "MOVE":
            if a[1].strip('"') != self.TRASH:
                return "NO", [b"[TRYCREATE] No folder (Failure)"]
            self.box[a[0]]["labels"] = {"\\Trash"}
            return "OK", [b""]
        if cmd == "STORE":
            op, labels = a[1], a[2]
            lab = labels.strip("()")
            if op == "-X-GM-LABELS":
                self.box[a[0]]["labels"].discard(lab)
            elif op == "+X-GM-LABELS":
                self.box[a[0]]["labels"] = {lab} if lab == "\\Trash" else self.box[a[0]]["labels"] | {lab}
            return "OK", [b""]
        raise AssertionError(cmd)

    def select(self, *a): return "OK", [b"1"]
    def noop(self): return "OK", [b""]
    def logout(self): pass


def gmail_box(emails, dry_run=False):
    from email_agent.mailbox import IMAPMailbox
    mb = IMAPMailbox.__new__(IMAPMailbox)
    mb.conn, mb.dry_run, mb.simulated, mb.folder = FakeGmail(emails), dry_run, dry_run, "INBOX"
    mb.gmail, mb.can_move = True, True
    special = mb._special_folders()
    mb.trash_folder, mb.archive_folder = special["trash"], special["all"]
    return mb


def test_localized_gmail_trash_really_moves(tmp_path):
    mb = gmail_box([E["d01"], E["d02"]])
    assert mb.trash_folder == FakeGmail.TRASH
    agent = EmailAgent(tmp_path, mb, HeuristicAnalyzer(), HeuristicInstructionParser(),
                       ScriptedUI({"d01": ["save the code to notes and delete", "1"]}), account="me")
    agent.run(days=None)
    assert mb.conn.inbox() == []                       # both really left the inbox


def test_refused_move_is_not_marked_handled(tmp_path):
    mb = gmail_box([E["d01"]])
    mb.trash_folder, mb.gmail = "[Gmail]/Trash", False  # wrong name and no Gmail fallback -> refused
    agent = EmailAgent(tmp_path, mb, HeuristicAnalyzer(), HeuristicInstructionParser(),
                       ScriptedUI({"d01": ["delete it", "3"]}), account="me")
    agent.run(days=None)
    assert "d01" not in agent.processed and mb.conn.inbox() == ["1"]
    assert "Couldn't" in agent.log.items[-1]["results"][-1]


def test_repair_of_earlier_unverified_moves(tmp_path):
    mb = gmail_box([E["d01"], E["d02"]])
    agent = EmailAgent(tmp_path, mb, HeuristicAnalyzer(), HeuristicInstructionParser(), ScriptedUI({}), account="me")
    for e in (E["d01"], E["d02"]):            # what an older version recorded: "moved to trash", but it wasn't
        ref = agent.originals.save(e, account="me")
        agent.originals.update(ref, location="trash", handled_by="rule r1")
        agent.processed.add(e.id)
    agent.run(days=None)
    assert mb.conn.inbox() == [] and all(x["verified"] for x in agent.originals.index)


# --------------------------------------------------------------------------- #
# Order: newest first, and mail that arrives during a session goes first
# --------------------------------------------------------------------------- #
def _dated(i, day, subj):
    return Email(f"n{i}", "Friend <f@example.com>", subj, "Just saying hi, talk soon.",
                 date=f"Sat, {day:02d} Sep 2026 10:00:00 -0400")


class RecordingUI(ScriptedUI):
    def __init__(self, answers, on_first_question=None):
        super().__init__(answers)
        self.order, self.fresh, self.hook = [], [], on_first_question

    def header(self, n, total, email, ref=None, fresh=False):
        self.order.append(email.id)
        self.fresh.append(fresh)
        super().header(n, total, email, ref, fresh)

    def ask_text(self, prompt):
        if self.hook:
            hook, self.hook = self.hook, None
            hook()
        return super().ask_text(prompt)


def test_newest_first(tmp_path):
    mails = [_dated(1, 20, "old"), _dated(2, 22, "middle"), _dated(3, 24, "newest")]
    mb = LocalMailbox(tmp_path / "mb"); mb.load_inbox(mails)
    ui = RecordingUI({m.id: ["leave it in my inbox", "3"] for m in mails})
    EmailAgent(tmp_path, mb, HeuristicAnalyzer(), HeuristicInstructionParser(), ui).run(days=None, newest_first=True)
    assert ui.order == ["n3", "n2", "n1"]


def test_new_arrival_jumps_the_queue(tmp_path):
    mails = [_dated(1, 20, "old"), _dated(2, 22, "middle")]
    late = _dated(9, 27, "arrived during the session")
    mb = LocalMailbox(tmp_path / "mb"); mb.load_inbox(mails)
    drop = lambda: mb._save("inbox", mb._load("inbox") + [late.to_dict()])
    ui = RecordingUI({i: ["leave it in my inbox", "3"] for i in ("n1", "n2", "n9")}, on_first_question=drop)
    stats = EmailAgent(tmp_path, mb, HeuristicAnalyzer(), HeuristicInstructionParser(), ui).run(
        days=None, newest_first=True, watch_arrivals=True)
    assert ui.order == ["n2", "n9", "n1"] and ui.fresh == [False, True, False]
    assert stats["asked"] == 3


def test_arrivals_respect_session_limit(tmp_path):
    mb = LocalMailbox(tmp_path / "mb"); mb.load_inbox([_dated(1, 20, "a"), _dated(2, 22, "b")])
    drop = lambda: mb._save("inbox", mb._load("inbox") + [_dated(8, 26, "x").to_dict(), _dated(9, 27, "y").to_dict()])
    ui = RecordingUI({i: ["leave it in my inbox", "3"] for i in ("n1", "n2", "n8", "n9")}, on_first_question=drop)
    stats = EmailAgent(tmp_path, mb, HeuristicAnalyzer(), HeuristicInstructionParser(), ui).run(
        limit=3, days=None, newest_first=True, watch_arrivals=True)
    assert len(ui.order) == 3 and ui.order[1] == "n9" and stats["left"] == 1


def test_imap_arrivals_by_uid(tmp_path):
    mb = gmail_box([E["d01"]])
    emails, _ = mb.fetch_new(30, None)
    assert [e.id for e in emails] == ["d01"] and mb.fetch_arrivals() == []
    mb.conn.box["2"] = {"mid": "late@x", "labels": {"\\Inbox"},
                        "raw": b"From: A <a@x.example>\r\nSubject: Late\r\nMessage-ID: late@x\r\n\r\nhi\r\n"}
    assert [e.subject for e in mb.fetch_arrivals()] == ["Late"] and mb.fetch_arrivals() == []


# --------------------------------------------------------------------------- #
# Speed: background reading, rule fast-path, instant simple answers, compact prompts
# --------------------------------------------------------------------------- #
import time as _time


class SlowAI(HeuristicAnalyzer):
    name = "local"

    def __init__(self, delay=0.4):
        self.delay, self.calls = delay, []

    def analyze(self, email):
        self.calls.append(email.id)
        _time.sleep(self.delay)
        a = super().analyze(email)
        a.engine = "local · test"
        return a


class ThinkingUI(ScriptedUI):
    """A user who takes a moment before answering."""
    def ask_text(self, prompt):
        _time.sleep(0.6)
        return super().ask_text(prompt)


def test_next_email_is_read_while_you_answer(tmp_path):
    mails = [_dated(1, 20, "a"), _dated(2, 22, "b"), _dated(3, 24, "c")]
    mb = LocalMailbox(tmp_path / "mb"); mb.load_inbox(mails)
    ai = SlowAI()
    ui = ThinkingUI({m.id: ["leave it in my inbox", "3"] for m in mails})
    agent = EmailAgent(tmp_path, mb, ai, HeuristicInstructionParser(), ui)
    seconds = []
    orig = ui.show_analysis
    ui.show_analysis = lambda a: (seconds.append(a.seconds), orig(a))
    agent.run(days=None, newest_first=True)
    assert seconds[0] >= 0.35 and all(s < 0.2 for s in seconds[1:])   # 2nd and 3rd were ready already
    assert sorted(ai.calls) == ["n1", "n2", "n3"]                        # each read exactly once


def test_known_sender_skips_the_ai(tmp_path):
    agent, mb = make_agent(tmp_path, [E["d01"]], {"d01": ["save the code to notes and delete", "1"]})
    agent.run(days=None)                                  # rule learned with the built-in reader
    ai = SlowAI()
    mb.load_inbox([E["d02"]])
    agent2 = EmailAgent(tmp_path, mb, ai, HeuristicInstructionParser(), ScriptedUI({}))
    assert agent2.run(days=None)["auto"] == 1 and ai.calls == []


def test_simple_answers_dont_wait_for_the_ai(tmp_path):
    from email_agent.local_ai import LocalInstructionParser
    p = LocalInstructionParser("none")
    p.model = "unreachable"             # any AI call would fail loudly in _chat; quick path must not call it
    a = HeuristicAnalyzer().analyze(E["d01"])
    assert [x["type"] for x in p.parse("Archive it", a)] == ["archive"]
    assert [x["type"] for x in p.parse("Unsubscribe and delete it", a)] == ["unsubscribe", "delete"]


def test_prompt_is_compact():
    from email_agent.local_ai import compact_body
    body = "Sale! " + "https://click.example.com/?id=" + "x" * 300 + "\n\n\n\n" + "More text. " * 1000
    out = compact_body(body)
    assert "[link 1: click.example.com]" in out and len(out) <= 3600 and "xxxxxxxx" not in out


# --------------------------------------------------------------------------- #
# Formatted (HTML) view of the original email
# --------------------------------------------------------------------------- #
def test_html_view_is_safe_and_shows_embedded_images():
    from email.message import EmailMessage
    from email_agent.render import csp, email_page
    m = EmailMessage(); m["From"] = "Shop <a@shop.example>"; m["Subject"] = "Hi"
    m.set_content("plain FALL20OFF")
    m.add_alternative('<p onclick="x()">Code <b>FALL20OFF</b></p><img src="cid:logo"><img src="https://t.example/o.gif">'
                      '<script>alert(1)</script><a href="javascript:alert(2)">x</a><form action="https://e.vil">', subtype="html")
    m.get_payload()[1].add_related(b"PNGDATA", maintype="image", subtype="png", cid="<logo>")
    page, info = email_page(bytes(m), "")
    assert info == {"has_html": True, "has_remote_images": True}
    assert "<script" not in page and "onclick" not in page and "javascript:" not in page and "<form" not in page
    assert "data:image/png;base64" in page and "<b>FALL20OFF</b>" in page
    assert "img-src data:;" in csp(False) and "https:" in csp(True) and "script-src 'none'" in csp(True)
    plain, _ = email_page(bytes(m), "", prefer_plain=True)
    assert "<b>" not in plain and "FALL20OFF" in plain
    text_only, info2 = email_page(None, "See https://x.org/a")
    assert info2["has_html"] is False and '<a href="https://x.org/a">' in text_only


def test_todos_done_undo_delete(tmp_path):
    from email_agent.stores import Todos
    t = Todos(tmp_path / "todos.json")
    a = t.add("Pay bill", None, "Con Ed", {}, "e1"); b = t.add("Call dentist", None, "Dental", {}, "e2")
    assert t.complete(a["id"]) and t.items[0]["done_at"]
    assert t.reopen(a["id"]) and not t.items[0]["done"]
    assert t.delete(b["id"]) and len(t.items) == 1
    assert t.add("New", None, "x", {}, "e3")["id"] == 2      # ids stay unique after deletes


def test_todo_titles_are_meaningful_and_dates_are_dates():
    from email_agent.agent import todo_title_and_due
    from email_agent.models import Analysis
    e = Email("x", "Maple Grove School <news@maplegrove.example>", "Friday Folder 9/25", "…")
    a = Analysis("newsletter", "Maple Grove weekly digest", "Maple Grove School", [], {"action_items": ["Download the Team App"],
                 "event": "Athletic games"}, 0.9, note_title="Friday Folder — Maple Grove School")
    assert todo_title_and_due(a, e) == ("Friday Folder — Maple Grove School", None)
    a.task_title, a.extracted["deadline"] = "Order class photos from Brightframe Studio by Sep 18", "9/18/2026"
    assert todo_title_and_due(a, e) == ("Order class photos from Brightframe Studio by Sep 18", "9/18/2026")


def test_key_link_survives_link_shortening():
    from email_agent.llm import analysis_from, prepare_body
    e = Email("p", "PayPal <service@paypal.com>", "Order on its way", "Track package (https://www.paypal.com/track/abc)")
    text, links = prepare_body(e.body, 3500)
    assert "[link 1: paypal.com]" in text
    a = analysis_from({"category": "shipping", "summary": "s", "sender_label": "PayPal", "features": [], "confidence": .9,
                       "key_link": 1, "key_link_label": "Track package", "extracted": {}}, e, "local", links)
    assert a.key_link == "https://www.paypal.com/track/abc" and a.key_link_label == "Track package"


def test_note_becomes_a_todo(tmp_path):
    from email_agent.webapp import App
    from email_agent.stores import Notes, Todos
    app = App(tmp_path, "offline", "x")
    app.mode = "gmail"   # use tmp_path, not the shared demo folder
    notes = Notes(app.data_dir / "notes.json")
    n = notes.add("Northwind", "20% off", "e1", "promotion", {"coupon_codes": ["SAVE20"], "expiry": "Oct 31"},
                  "Coupon for 20% off", ref=3, title="Northwind 20% coupon", link="https://northwind.example/x",
                  link_label="Shop")
    r = app.note_to_todo(n["id"])
    t = Todos(app.data_dir / "todos.json").items[0]
    assert r["title"] == t["title"] == "Northwind 20% coupon" and not r["already"]
    assert t["due"] == "Oct 31" and t["link"] == "https://northwind.example/x" and t["ref"] == 3
    assert t["details"]["coupon_codes"] == ["SAVE20"]
    # the note stays and knows it's in to-dos; converting twice doesn't duplicate
    p = app.panels()
    assert p["notes"][0]["in_todos"] and len(p["todos"]) == 1
    assert app.note_to_todo(n["id"])["already"] and len(Todos(app.data_dir / "todos.json").items) == 1
    # delete the to-do: the note can be converted again
    app.todo_delete(r["todo_id"])
    assert not app.panels()["notes"][0]["in_todos"]
    # an edited note carries your text
    app.edit_note(n["id"], "Use SAVE20 on the winter jacket\nonly online", None)
    app.note_to_todo(n["id"])
    t = Todos(app.data_dir / "todos.json").items[-1]
    assert t["about"] == "Use SAVE20 on the winter jacket" and "winter jacket" in t["details"]["note"]


# --------------------------------------------------------------------------- #
# Auto mode: whole inbox, no questions
# --------------------------------------------------------------------------- #
def test_auto_mode_goes_through_everything_without_asking(tmp_path):
    agent, mb = make_agent(tmp_path, [E["d01"]], {"d01": ["save the code to notes and delete", "1"]})
    agent.run(days=None)
    # a big inbox, read in small batches; ScriptedUI has no answers, so any question would fail the test
    mb.load_inbox([E["d02"], E["d03"], E["d07"], E["d10"], E["d11"], E["d12"]])
    auto = EmailAgent(tmp_path, mb, HeuristicAnalyzer(), HeuristicInstructionParser(), ScriptedUI({}))
    stats = auto.run_auto(batch=2)
    assert stats["auto"] == 1 and stats["skipped"] == 5 and not stats.get("stopped")
    assert mb.counts()["inbox"] == 5 and mb.counts()["trash"] == 1       # skipped ones stay in the inbox
    assert "d03" not in auto.processed and "d02" in auto.processed      # ...and come up next session
    again = EmailAgent(tmp_path, mb, HeuristicAnalyzer(), HeuristicInstructionParser(), ScriptedUI({}))
    assert again.run_auto()["skipped"] == 5                             # nothing lost, nothing looped


def test_auto_mode_read_first_rule_saves_note_and_archives(tmp_path):
    agent, mb = make_agent(tmp_path, SEC[:1], {"sec0": [SEC_TEXT, "2", "1"]})
    agent.run(days=None)
    mb.load_inbox(SEC[1:])
    auto = EmailAgent(tmp_path, mb, HeuristicAnalyzer(), HeuristicInstructionParser(), ScriptedUI({}))
    assert auto.run_auto()["auto"] == 1
    assert mb.counts() == {"inbox": 0, "archive": 1, "trash": 0}        # archived, not deleted
    assert auto.notes.items and auto.notes.items[-1]["email_id"] == "sec1"
    assert [a["type"] for a in auto.rules.rules[0].actions] == ["show", "delete"]   # rule unchanged


def test_auto_mode_practice_does_not_loop(tmp_path):
    agent, mb = make_agent(tmp_path, [E["d01"]], {"d01": ["save the code to notes and delete", "1"]})
    agent.run(days=None)
    mb.load_inbox([E["d02"], E["d03"], E["d04"]])
    mb.dry_run = mb.simulated = True
    auto = EmailAgent(tmp_path, mb, HeuristicAnalyzer(), HeuristicInstructionParser(), ScriptedUI({}))
    stats = auto.run_auto(batch=1)
    assert stats["auto"] + stats["skipped"] == 3 and mb.counts()["inbox"] == 3


# --------------------------------------------------------------------------- #
# Clean-up of old notes and to-dos
# --------------------------------------------------------------------------- #
def test_cleanup_by_email_age_keeps_upcoming_and_undoes(tmp_path):
    from datetime import date, datetime, timedelta
    from email.utils import format_datetime
    from email_agent import cleanup
    from email_agent.stores import Notes, Originals, Todos
    notes, todos, orig = Notes(tmp_path / "notes.json"), Todos(tmp_path / "todos.json"), Originals(tmp_path / "originals")
    old_date = format_datetime(datetime.now().astimezone() - timedelta(days=100))
    new_date = format_datetime(datetime.now().astimezone() - timedelta(days=3))
    r_old = orig.save(Email("o1", "A <a@x.example>", "old", "b", date=old_date))
    r_new = orig.save(Email("n1", "B <b@x.example>", "new", "b", date=new_date))
    notes.add("A", "old", "o1", "promotion", {}, "s", r_old)
    notes.add("B", "new", "n1", "promotion", {}, "s", r_new)
    ahead = (date.today() + timedelta(days=20)).strftime("%B %d, %Y")
    todos.add("Old, past", "January 3, 2020", "A", {}, "o1", r_old)
    todos.add("Old, still ahead", ahead, "A", {}, "o1", r_old)
    todos.add("New", None, "B", {}, "n1", r_new)
    no_year = (date.today() - timedelta(days=80)).strftime("%b %d")       # "Jul 12" from a 100-day-old email
    todos.add("Old, no year", no_year, "A", {}, "o1", r_old)
    p = cleanup.plan(todos, notes, orig, 30)
    assert len(p["notes"]) == 1 and len(p["todos"]) == 2 and p["kept_upcoming"] == 1
    assert len(cleanup.plan(todos, notes, orig, 30, keep_upcoming=False)["todos"]) == 3
    r = cleanup.run(tmp_path, todos, notes, orig, 30)
    assert r["removed_notes"] == 1 and [t["title"] for t in todos.items] == ["Old, still ahead", "New"]
    assert [n["subject"] for n in Notes(tmp_path / "notes.json").items] == ["new"]     # saved to disk
    notes.add("C", "made after", "c1", "promotion", {}, "s")                             # may reuse an id
    back = cleanup.undo(tmp_path, todos, notes)
    assert back == {"notes": 1, "todos": 2} and len(notes.items) == 3
    assert len({n["id"] for n in notes.items}) == 3 and cleanup.last_batch(tmp_path) is None


def test_parse_due_dates():
    from datetime import date
    from email_agent.cleanup import parse_due
    t = date(2026, 9, 30)
    assert parse_due("October 15, 2026", t) == date(2026, 10, 15)
    assert parse_due("by Oct 3", t) == date(2026, 10, 3)
    assert parse_due("Jan 5", t) == date(2027, 1, 5)          # no year: the next one
    assert parse_due("10/15/2026", t) == date(2026, 10, 15) and parse_due("2026-10-15", t) == date(2026, 10, 15)
    assert parse_due("tomorrow-ish", t) is None and parse_due("Feb 31", t) is None


def test_todo_date_line():
    from datetime import date
    from email_agent.cleanup import todo_when
    t = date(2026, 9, 30)
    w = todo_when({"due": "Oct 3 at 5pm"}, t, t)
    assert w == {"date": "2026-10-03", "days": 3, "text": "", "time": "5pm"}
    assert todo_when({"due": "September 28, 2026"}, t, t)["days"] == -2                       # overdue
    assert "meeting" in todo_when({"due": "Board meeting on 24 Oct 2026 at 7:00pm"}, t, t)["text"]
    assert todo_when({"title": "Sign up by 3 Oct"}, t, t)["date"] == "2026-10-03"           # date in the title
    assert todo_when({"due": "Immediate"}, t, t) == {"date": None, "text": "Immediate"}     # shown as written
    assert todo_when({"title": "You may 5 times pay"}, t, t) is None and todo_when({"title": "Pay"}, t, t) is None


def test_sidebar_lists_newest_email_first(tmp_path):
    from datetime import datetime, timedelta
    from email.utils import format_datetime
    from email_agent.webapp import App
    from email_agent.stores import Notes, Originals, Todos
    app = App(tmp_path, "offline", "x")
    app.mode = "gmail"
    orig = Originals(tmp_path / "originals")
    notes, todos = Notes(tmp_path / "notes.json"), Todos(tmp_path / "todos.json")
    # handled in this order, but sent in a different order
    for i, age in enumerate([5, 400, 1]):
        ref = orig.save(Email(f"e{i}", "A <a@x.example>", f"sent {age} days ago", "b",
                              date=format_datetime(datetime.now().astimezone() - timedelta(days=age))))
        notes.add("A", f"sent {age} days ago", f"e{i}", "promotion", {}, "s", ref)
        todos.add(f"sent {age} days ago", None, "A", {}, f"e{i}", ref)
    p = app.panels()
    order = ["sent 1 days ago", "sent 5 days ago", "sent 400 days ago"]
    assert [n["subject"] for n in p["notes"]] == order and [t["title"] for t in p["todos"]] == order
    assert all(r["sent"] for r in p["todos"])


def test_mail_agent_command_defaults_to_app_and_has_version(capsys):
    from email_agent import __version__
    from email_agent.cli import main, with_default_command
    cmds = {"app": None, "demo": None, "run": None}
    assert with_default_command([], cmds) == ["app"]
    assert with_default_command(["--data-dir", "/x"], cmds) == ["--data-dir", "/x", "app"]
    assert with_default_command(["--port", "9000", "--no-browser"], cmds) == ["app", "--port", "9000", "--no-browser"]
    assert with_default_command(["--data-dir=/x", "--port", "1"], cmds) == ["--data-dir=/x", "app", "--port", "1"]
    assert with_default_command(["demo", "--interactive"], cmds) == ["demo", "--interactive"]
    assert with_default_command(["--help"], cmds) == ["--help"]
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0 and __version__ in capsys.readouterr().out


def test_demo_data_is_kept_out_of_the_current_folder():
    import tempfile
    from email_agent import cli
    src = Path(cli.__file__).read_text()
    assert "./demo_data" not in src and "email_agent_demo_cli" in src and tempfile.gettempdir()


def test_demo_dates_are_relative_to_today():
    from datetime import date
    from email_agent.demo import DEMO_EMAILS, on
    dates = [e.date for e in DEMO_EMAILS]
    assert dates == sorted(dates) and dates[-1] == date.today().isoformat()   # newest arrived today
    bill = next(e for e in DEMO_EMAILS if e.id == "d08")
    assert on(8) in bill.body                                                 # due in 8 days, never in the past


def test_email_dates_read_both_header_and_plain_dates():
    from email_agent.cleanup import _parse_sent
    assert _parse_sent("Fri, 6 Nov 2026 10:00:00 -0500").day == 6
    assert _parse_sent("2026-11-06").date().isoformat() == "2026-11-06"
    assert _parse_sent("not a date") is None and _parse_sent("") is None


# --------------------------------------------------------------------------- #
# The double-click app: one copy at a time, Quit, quit when idle
# --------------------------------------------------------------------------- #
def test_app_runs_once_quits_from_the_page_and_when_idle(tmp_path):
    import threading, time, urllib.request
    from email_agent import webapp
    port = 18765
    t = threading.Thread(target=webapp.serve, args=(tmp_path,), kwargs={"port": port, "engine": "offline",
                                                                         "open_browser": False}, daemon=True)
    t.start()
    for _ in range(150):   # slow machines (GitHub's Mac runners) can take a few seconds
        if webapp.find_running(port, span=1):
            break
        time.sleep(0.1)
    url = webapp.find_running(port, span=1)
    assert url == f"http://127.0.0.1:{port}/"
    # a second launch doesn't start another server: it returns at once
    second = threading.Thread(target=webapp.serve, args=(tmp_path,), kwargs={"port": port, "engine": "offline",
                                                                              "open_browser": False}, daemon=True)
    second.start(); second.join(timeout=5)
    assert not second.is_alive()
    req = urllib.request.Request(url + "api/quit", data=b"{}", method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        assert r.status == 200
    t.join(timeout=10)
    assert not t.is_alive() and webapp.find_running(port, span=1) is None
    # idle rule: only when no page has been seen for the whole time and nothing is running
    app = webapp.App(tmp_path, "offline", "x")
    now = app.last_seen + 31 * 60
    assert webapp.idle_for_too_long(app, 30, now) and not webapp.idle_for_too_long(app, 30, app.last_seen + 60)
    app.status = "running"
    assert not webapp.idle_for_too_long(app, 30, now)


def test_mac_launcher_opens_the_app(tmp_path, monkeypatch):
    import importlib.util
    from email_agent import cli
    seen = []
    monkeypatch.setattr(cli, "main", lambda argv=None: seen.append(argv))
    monkeypatch.setenv("EMAIL_AGENT_DATA", str(tmp_path))
    monkeypatch.setenv("MAIL_AGENT_NO_BROWSER", "1")
    spec = importlib.util.spec_from_file_location("launcher", Path(__file__).parent.parent / "packaging" / "mac" / "launcher.py")
    launcher = importlib.util.module_from_spec(spec); spec.loader.exec_module(launcher)
    launcher.main()
    assert seen == [["app", "--quit-when-idle", "30", "--no-browser"]]


# --------------------------------------------------------------------------- #
# "Try again now" for a move the server refused
# --------------------------------------------------------------------------- #
class FlakyMailbox(LocalMailbox):
    """Refuses to archive until told otherwise; knows how to find mail in the inbox like the IMAP one does."""
    refuse = True

    def find_in_inbox(self, message_id):
        return "1" if any(d["id"] == message_id for d in self._load("inbox")) else None

    def archive(self, msg):
        if self.refuse:
            raise RuntimeError("server said no")
        return super().archive(msg)


def test_failed_move_can_be_retried(tmp_path):
    from email_agent.agent import retry_failed_move
    mb = FlakyMailbox(tmp_path / "mailbox")
    mb.load_inbox([E["d12"]])
    agent = EmailAgent(tmp_path, mb, HeuristicAnalyzer(), HeuristicInstructionParser(), ScriptedUI({"d12": ["v", "r"]}))
    agent.run(days=None)
    ref = agent.originals.ref_for("d12")
    entry = agent.originals.entry(ref)
    assert entry["handled_by"].endswith("— failed") and entry["wanted"] == "archive"
    assert mb.counts()["inbox"] == 1 and "d12" not in agent.processed
    with pytest.raises(RuntimeError):                      # still refused: nothing changes
        retry_failed_move(mb, agent.originals, entry, tmp_path / "processed.json")
    assert agent.originals.entry(ref)["handled_by"].endswith("— failed")
    mb.refuse = False
    assert retry_failed_move(mb, agent.originals, entry, tmp_path / "processed.json") == "archived"
    after = agent.originals.entry(ref)
    assert after["location"] == "archive" and after["verified"] and not after["handled_by"].endswith("failed")
    assert "d12" in json.loads((tmp_path / "processed.json").read_text())
    with pytest.raises(ValueError):                        # nothing left to retry
        retry_failed_move(mb, agent.originals, after, tmp_path / "processed.json")
