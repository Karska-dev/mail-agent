"""Command line entry point.

  python -m email_agent app                   open the agent in your browser (easiest)
  python -m email_agent demo                  scripted walkthrough on a sample inbox
  python -m email_agent demo --interactive    same inbox, you answer the questions
  python -m email_agent run                   process a real IMAP inbox (see README)
  python -m email_agent rules [--delete ID]   see / forget learned rules
  python -m email_agent notes | todos [--done N]
  python -m email_agent history [TEXT]        every email the agent handled, with its #number
  python -m email_agent show N [--eml]        read the original email #N
  python -m email_agent restore N             put email #N back in your inbox
  python -m email_agent read                  digest of one-off "read & archive" emails
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from pathlib import Path

from .agent import EmailAgent
from .demo import DEMO_ANSWERS, DEMO_EMAILS
from .llm import DEFAULT_MODEL, make_engines
from .mailbox import IMAPMailbox, LocalMailbox
from .rules import RuleStore, explain_rule
from .stores import Notes, Originals, ReadLog, Todos
from .ui import ConsoleUI, ScriptedUI, bold, dim


def _data_dir(args) -> Path:
    return Path(args.data_dir).expanduser()


def cmd_demo(args) -> None:
    data = _data_dir(args)
    if data.exists():
        shutil.rmtree(data)  # the demo always starts from a fresh sample inbox
    mailbox = LocalMailbox(data / "mailbox")
    mailbox.load_inbox(DEMO_EMAILS)
    analyzer, parser = make_engines(args.engine, args.model, args.local_model)
    ui = ConsoleUI() if args.interactive else ScriptedUI(DEMO_ANSWERS)
    agent = EmailAgent(data, mailbox, analyzer, parser, ui)
    print(bold(f"Email agent demo — {len(DEMO_EMAILS)} emails · engine: {analyzer.name} · data: {data}"))
    stats = agent.run(limit=args.limit, days=args.days or None)
    print()
    print(dim("═" * 72))
    print(bold("Summary"))
    print(f"  handled automatically: {stats['auto']} · asked you: {stats['asked']} · "
          f"read & archived (no rule): {stats['one_off']} · skipped: {stats['skipped']} · "
          f"left for next time: {stats['left']}")
    print(f"  mailbox: {mailbox.counts()}")
    print()
    print(bold("Learned rules"))
    print(agent.describe_rules())
    print()
    print(bold("To-dos"))
    _print_todos(agent.todos)
    print()
    print(bold(f"Notes → {data / 'notes.md'}"))
    print((data / "notes.md").read_text() if (data / "notes.md").exists() else "(none)")
    print(bold(f"Read & archived → {data / 'read_log.md'}"))
    print((data / "read_log.md").read_text() if (data / "read_log.md").exists() else "(none)")
    print(bold("Originals"))
    _print_history(agent.originals.index)
    print(dim(f"\n  Review any of them:  python -m email_agent --data-dir {data} show 12"))


def cmd_app(args) -> None:
    from .webapp import serve
    serve(_data_dir(args), port=args.port, engine=args.engine, model=args.model, open_browser=not args.no_browser,
          quit_when_idle=args.quit_when_idle)


def cmd_run(args) -> None:
    host = args.host or os.environ.get("EMAIL_AGENT_IMAP_HOST", "imap.gmail.com")
    user = args.user or os.environ.get("EMAIL_AGENT_USER")
    password = os.environ.get("EMAIL_AGENT_PASSWORD")
    if user and not password:
        from . import keychain
        password = keychain.get(f"imap:{user.lower()}")  # saved from the app
    if not (user and password):
        raise SystemExit("Pass --user you@gmail.com for a mailbox saved in the app, or set "
                         "EMAIL_AGENT_USER and EMAIL_AGENT_PASSWORD (for Gmail: an App Password).")
    mailbox = IMAPMailbox(host, user, password, dry_run=args.dry_run)
    analyzer, parser = make_engines(args.engine, args.model, args.local_model)
    agent = EmailAgent(_data_dir(args), mailbox, analyzer, parser, ConsoleUI(), args.min_confidence,
                       interactive=not (args.rules_only or args.auto))
    mode = " · DRY RUN" if args.dry_run else ""
    mode += " · AUTO: whole inbox, no questions" if args.auto else ""
    mode += " · rules only (unknown mail waits for you)" if args.rules_only else ""
    print(bold(f"Checking {user} · engine: {analyzer.name}{mode}"))
    try:
        while True:
            if args.auto:
                stats = agent.run_auto(include_read=args.include_read)
            else:
                stats = agent.run(limit=args.limit, days=args.days or None, include_read=args.include_read,
                                  newest_first=True, watch_arrivals=True)
            print(f"\nDone: {stats['auto']} automatic · {stats['asked']} asked · "
                  f"{stats['one_off']} read & archived · {stats['skipped']} skipped · "
                  f"{stats['left']} left for next time")
            if stats.get("stopped") or not args.watch:
                break
            time.sleep(args.watch * 60)
            mailbox.conn.noop()
    finally:
        mailbox.close()


def _print_history(rows) -> None:
    if not rows:
        print("  (none)")
    for e in rows:
        where = {"inbox": "inbox  ", "archive": "archive", "trash": "trash  "}.get(e["location"], e["location"])
        print(f"  #{e['ref']:<4} {where}  {e['from'][:32]:<32}  {e['subject'][:44]:<44}  "
              + dim(e.get("handled_by") or "waiting for you"))


def cmd_history(args) -> None:
    rows = Originals(_data_dir(args) / "originals").search(args.text)
    _print_history(rows[-args.n:])


def cmd_show(args) -> None:
    originals = Originals(_data_dir(args) / "originals")
    entry = originals.entry(args.ref)
    if not entry:
        raise SystemExit(f"No email #{args.ref}. See `history`.")
    if args.eml:
        path = originals.eml_path(args.ref)
        print(path if path else "No raw .eml for this one (only saved for IMAP mail).")
        return
    email = originals.load(args.ref)
    print(dim(f"#{entry['ref']} · now in: {entry['location']} · handled by: {entry.get('handled_by') or '—'}"))
    ConsoleUI().show_full(email, args.ref)
    if entry.get("has_eml"):
        print(dim(f"   Raw message (opens in any mail app): {originals.eml_path(args.ref)}"))


def _open_mailbox(args):
    local = _data_dir(args) / "mailbox"
    if local.exists():
        return LocalMailbox(local)
    host = os.environ.get("EMAIL_AGENT_IMAP_HOST", "imap.gmail.com")
    user, password = os.environ.get("EMAIL_AGENT_USER"), os.environ.get("EMAIL_AGENT_PASSWORD")
    if not (user and password):
        raise SystemExit("Set EMAIL_AGENT_USER and EMAIL_AGENT_PASSWORD to restore mail on the server.")
    return IMAPMailbox(host, user, password)


def cmd_restore(args) -> None:
    originals = Originals(_data_dir(args) / "originals")
    entry = originals.entry(args.ref)
    if not entry:
        raise SystemExit(f"No email #{args.ref}. See `history`.")
    mailbox = _open_mailbox(args)
    try:
        result = mailbox.restore(originals.load(args.ref))
    finally:
        mailbox.close()
    if result.startswith("moved back") or result.startswith("already"):
        originals.update(args.ref, location="inbox")
    print(f"#{args.ref}: {result}")


def cmd_read(args) -> None:
    log = ReadLog(_data_dir(args) / "read_log.json")
    log.render()
    print(log.path.with_suffix(".md").read_text())


def cmd_rules(args) -> None:
    store = RuleStore(_data_dir(args) / "rules.json")
    if args.delete:
        print("Deleted." if store.delete(args.delete) else f"No rule {args.delete}.")
        return
    print("\n\n".join(explain_rule(r) for r in store.rules) or "No rules learned yet.")


def _print_todos(todos: Todos) -> None:
    if not todos.items:
        print("  (none)")
    for t in todos.items:
        box = "[x]" if t["done"] else "[ ]"
        due = f"  (due {t['due']})" if t.get("due") else ""
        src = t["source"] + (f", email #{t['ref']}" if t.get("ref") else "")
        print(f"  {t['id']:>2}. {box} {t['title']}{due}  {dim('— ' + src)}")


def cmd_todos(args) -> None:
    todos = Todos(_data_dir(args) / "todos.json")
    if args.done:
        print("Marked done." if todos.complete(args.done) else f"No to-do {args.done}.")
    _print_todos(todos)


def cmd_notes(args) -> None:
    notes = Notes(_data_dir(args) / "notes.json")
    notes.render()
    print(notes.path.with_suffix(".md").read_text())


def cmd_log(args) -> None:
    path = _data_dir(args) / "activity_log.json"
    for e in (json.loads(path.read_text()) if path.exists() else [])[-args.n:]:
        print(f"{e['at']}  #{e.get('ref') or '-':<4} {e['from'][:30]:<30}  {e['subject'][:40]:<40}  {e['source']}: {', '.join(e['results'])}")


def with_default_command(argv: list[str], commands, default: str = "app") -> list[str]:
    """`mail-agent` on its own (or with only options) opens the app."""
    if any(a in commands for a in argv) or any(a in ("-h", "--help", "--version") for a in argv):
        return argv
    i = 0                      # keep the global options first: mail-agent --data-dir X --port 9000
    while i < len(argv) and argv[i].startswith("--data-dir"):
        i += 1 if "=" in argv[i] else 2
    return argv[:i] + [default] + argv[i:]


def main(argv=None) -> None:
    from . import __version__
    p = argparse.ArgumentParser(prog="mail-agent", description="A personal email agent that learns your rules. "
                                "Run it with no command to open the app in your browser.")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--data-dir", default=os.environ.get("EMAIL_AGENT_DATA", "~/.email_agent"),
                   help="where rules, notes and to-dos live (default ~/.email_agent)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def engine_opts(sp, default="auto"):
        sp.add_argument("--engine", choices=["auto", "claude", "local", "offline"], default=default,
                        help="auto = Claude if ANTHROPIC_API_KEY is set, else built-in; local = Ollama on this Mac")
        sp.add_argument("--model", default=DEFAULT_MODEL, help="Claude model")
        sp.add_argument("--local-model", default=None, help="Ollama model, e.g. qwen2.5:7b")

    ap = sub.add_parser("app", help="open the agent in your web browser")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true", help="don't open the browser automatically")
    ap.add_argument("--quit-when-idle", type=float, metavar="MINUTES",
                    help="quit by itself when no page has been open for this long and nothing is running")
    engine_opts(ap)
    ap.set_defaults(func=cmd_app)

    d = sub.add_parser("demo", help="walk through the sample inbox")
    d.add_argument("--interactive", action="store_true", help="answer the questions yourself")
    d.add_argument("--limit", type=int, default=30, help="max emails per session (default 30)")
    d.add_argument("--days", type=int, default=0, help="only mail from the last N days (default: all demo mail)")
    engine_opts(d, default="offline")
    d.set_defaults(func=cmd_demo, data_dir_override=str(Path(tempfile.gettempdir()) / "email_agent_demo_cli"))

    r = sub.add_parser("run", help="process new mail in an IMAP inbox")
    r.add_argument("--host")
    r.add_argument("--user")
    r.add_argument("--limit", type=int, default=30, help="max emails per session (default 30)")
    r.add_argument("--days", type=int, default=14, help="only mail from the last N days (default 14, 0 = all)")
    r.add_argument("--dry-run", action="store_true", help="analyze and learn, but don't move/delete mail")
    r.add_argument("--min-confidence", type=float, default=0.6)
    r.add_argument("--include-read", action="store_true",
                   help="go through everything in the inbox, not just unread mail (for clearing it out)")
    r.add_argument("--rules-only", action="store_true",
                   help="unattended mode: apply learned rules, never ask (unknown mail is left for later)")
    r.add_argument("--auto", action="store_true",
                   help="auto mode: go through the whole inbox (no day/count limits), never ask; emails that "
                        "need you stay unread for next session; 'read it first' rules save a note and archive")
    r.add_argument("--watch", type=float, metavar="MINUTES", help="keep running, checking every N minutes")
    engine_opts(r)
    r.set_defaults(func=cmd_run)

    ru = sub.add_parser("rules", help="list or delete learned rules")
    ru.add_argument("--delete", metavar="ID")
    ru.set_defaults(func=cmd_rules)

    t = sub.add_parser("todos", help="show to-dos")
    t.add_argument("--done", type=int, metavar="N")
    t.set_defaults(func=cmd_todos)

    sub.add_parser("notes", help="show notes").set_defaults(func=cmd_notes)
    sub.add_parser("read", help="digest of emails you had read & archived without a rule").set_defaults(func=cmd_read)

    h = sub.add_parser("history", help="every email the agent has handled, with its #number")
    h.add_argument("text", nargs="?", help="filter by sender or subject")
    h.add_argument("-n", type=int, default=50)
    h.set_defaults(func=cmd_history)

    sh = sub.add_parser("show", help="read the original of email #N")
    sh.add_argument("ref", type=int)
    sh.add_argument("--eml", action="store_true", help="print the path of the raw .eml file instead")
    sh.set_defaults(func=cmd_show)

    rs = sub.add_parser("restore", help="move email #N back to your inbox")
    rs.add_argument("ref", type=int)
    rs.set_defaults(func=cmd_restore)
    lg = sub.add_parser("log", help="recent activity")
    lg.add_argument("-n", type=int, default=30)
    lg.set_defaults(func=cmd_log)

    argv = list(__import__("sys").argv[1:] if argv is None else argv)
    args = p.parse_args(with_default_command(argv, sub.choices))
    # the demo keeps its data separate from your real data unless --data-dir is given explicitly
    if getattr(args, "data_dir_override", None) and "--data-dir" not in argv:
        args.data_dir = args.data_dir_override
    args.func(args)


if __name__ == "__main__":
    main()
