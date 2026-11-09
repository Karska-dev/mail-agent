"""How the agent talks to the user: a terminal UI and a scripted UI (demo/tests)."""
from __future__ import annotations

import re
import shutil
import sys
import textwrap

from .models import EXTRACT_FIELDS, Analysis, Email

_TTY = sys.stdout.isatty()


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m" if _TTY else s


bold = lambda s: _c("1", s)      # noqa: E731
dim = lambda s: _c("2", s)       # noqa: E731
green = lambda s: _c("32", s)    # noqa: E731
yellow = lambda s: _c("33", s)   # noqa: E731
cyan = lambda s: _c("36", s)     # noqa: E731

# a coloured label per category, so you can tell emails apart at a glance
_CAT_COLORS = {"promotion": "45", "newsletter": "44", "bill": "41", "receipt": "42", "shipping": "46",
               "appointment": "43", "security": "41", "social": "44", "work": "46", "personal": "42"}


def badge(category: str) -> str:
    text = f" {category.upper()} "
    return _c(f"1;97;{_CAT_COLORS.get(category, '100')}", text) if _TTY else f"[{category.upper()}]"


def _width() -> int:
    return max(60, min(100, shutil.get_terminal_size((88, 24)).columns))


STOP_RE = re.compile(r"^\s*(?:q|quit|exit|stop|finish|pause|done|stop for now|finish for now|done for now|"
                     r"that'?s all(?: for now)?|continue later|read (?:the rest )?later)\s*[.!]?\s*$", re.I)


class StopSession(Exception):
    """The user wants to finish now; everything not yet handled stays in the inbox for next time."""


class ConsoleUI:
    """Terminal output laid out as one 'card' per email:

        ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
         3 of 12  #3                                              PROMOTION
         ThermaLayer is back — discover the new collection
         Northwind <news@mail.northwind.example> · 2026-09-23

         Discover the new ThermaLayer collection, now warmer and softer than ever.
           coupon code   FALL20OFF
         ? Your rule ... doesn't fully apply
         ✓ archived
    """

    def info(self, text: str = "") -> None:
        print(text)

    def header(self, n: int, total: int, email: Email, ref: int | None = None, fresh: bool = False) -> None:
        w = _width()
        print()
        print(cyan("━" * w))
        print(dim(f" {n} of {total}" + (f"   #{ref}" if ref else "")) + (green("   📨 just arrived") if fresh else ""))
        print(" " + bold(email.subject))
        print(dim(" " + email.sender + (f" · {email.date}" if email.date else "")))

    def show_full(self, email: Email, ref: int | None = None) -> None:
        w = _width() - 6
        print()
        print(dim("   ┌─ original email" + (f" #{ref} " if ref else " ") + "─" * max(4, w - 20)))
        print(dim("   │ ") + bold("From:    ") + email.sender)
        print(dim("   │ ") + bold("Subject: ") + email.subject)
        if email.date:
            print(dim("   │ ") + bold("Date:    ") + email.date)
        print(dim("   │"))
        for line in email.body.splitlines() or [""]:
            for chunk in textwrap.wrap(line, w) or [""]:
                print(dim("   │ ") + chunk)
        print(dim("   └" + "─" * w))
        print()

    def show_reading(self, email: Email, ref: int | None = None) -> None:
        """The full email, for rules that say 'let me read it first'."""
        self.show_full(email, ref)

    def show_analysis(self, a: Analysis) -> None:
        w = _width()
        print()
        tags = ""
        if a.seconds >= 1:
            tags += dim(f"  ({a.seconds:.0f}s)")
        if a.importance == "high":
            tags += "  " + _c("1;33", "★ IMPORTANT") if _TTY else "  [IMPORTANT]"
        print(" " + badge(a.category) + tags + "  " + a.summary)
        if a.risk in ("high", "low"):
            head = ("⚠  BE CAREFUL — this could be a fake (phishing) email" if a.risk == "high"
                    else "⚠  Check this one before acting")
            print("   " + _c("1;31" if a.risk == "high" else "1;33", head))
            for sign in a.risk_signs:
                print("     - " + sign)
            print(dim("     Don't click its links. If unsure, open the website yourself and check there."))
        elif a.category == "security":
            print("   " + _c("1;34", "🔒 Security email") + dim(" — if unexpected, check your account on the website "
                                                              "itself, not via the email's links."))
        elif a.importance == "high" and a.importance_reason:
            print("   " + yellow("★ " + a.importance_reason))
        vals = {EXTRACT_FIELDS.get(k, k): v for k, v in a.values().items()
                if k not in ("links", "dates", "action_items", "amounts")}
        if vals:
            label_w = max(len(k) for k in vals) + 2
            for k, v in vals.items():
                items = v if isinstance(v, list) else [v]
                for i, item in enumerate(items):
                    label = k if i == 0 else ""
                    text = bold(str(item)) if k in ("coupon code", "amount due", "deadline", "when") else str(item)
                    wrapped = textwrap.wrap(str(item), w - label_w - 6) or [""]
                    first = text if len(wrapped) == 1 else wrapped[0]
                    print("   " + dim(label.ljust(label_w)) + first)
                    for more in (wrapped[1:] if len(wrapped) > 1 else []):
                        print("   " + " " * label_w + more)
        print()

    def done(self, text: str) -> None:
        print(" " + green("✓ ") + text)

    def notice(self, text: str) -> None:
        for i, line in enumerate(textwrap.wrap(text, _width() - 4)):
            print(" " + (yellow("? ") if i == 0 else "  ") + yellow(line))

    def ask_text(self, prompt: str) -> str:
        print()
        print(" " + bold(cyan("› ")) + bold(prompt))
        print(dim("   shortcuts:  v view original · r read & archive, no rule · s skip · q stop for now"))
        ans = input(bold(cyan("   > "))).strip()
        if STOP_RE.match(ans):
            raise StopSession
        return ans

    def choose(self, prompt: str, options: list[str]) -> int:
        print()
        print(" " + bold(cyan("› ")) + bold(prompt))
        for i, o in enumerate(options, 1):
            extra = o.startswith(("Just read it", "Show me the full"))
            line = f"   {cyan(bold(f'{i:>2}'))}  {o}"
            print(dim(line) if extra else line)
        print(dim("   type a number · q stop for now"))
        while True:
            raw = input(bold(cyan("   > "))).strip()
            if STOP_RE.match(raw):
                raise StopSession
            if raw.isdigit() and 1 <= int(raw) <= len(options):
                return int(raw) - 1
            print(dim(f"   Please type a number 1–{len(options)}."))


class ScriptedUI(ConsoleUI):
    """Replays pre-written answers per email id, printing them as if typed."""

    def __init__(self, answers: dict[str, list[str]]):
        self.answers = {k: list(v) for k, v in answers.items()}
        self.current: str | None = None

    def set_email(self, email_id: str) -> None:
        self.current = email_id

    def _next(self) -> str:
        queue = self.answers.get(self.current or "", [])
        if not queue:
            raise RuntimeError(f"scripted demo has no answer left for {self.current}")
        return queue.pop(0)

    def ask_text(self, prompt: str) -> str:
        ans = self._next()
        print()
        print(" " + bold(cyan("› ")) + bold(prompt))
        print(f"   {cyan('>')} {ans}")
        if STOP_RE.match(ans):
            raise StopSession
        return ans

    def choose(self, prompt: str, options: list[str]) -> int:
        ans = self._next()
        print()
        print(" " + bold(cyan("› ")) + bold(prompt))
        for i, o in enumerate(options, 1):
            print(f"   {cyan(bold(f'{i:>2}'))}  {o}")
        print(f"   {cyan('>')} {ans}")
        if STOP_RE.match(ans):
            raise StopSession
        return int(ans) - 1
