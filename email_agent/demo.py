"""A sample inbox that walks through the whole learning story, plus scripted answers.

Dates are relative to today, so the demo always looks current: the newest email arrived today,
and offers, bills and appointments fall in the days ahead.
"""
from __future__ import annotations

from datetime import date, timedelta

from .models import Email

TODAY = date.today()


def sent(days_ago: int) -> str:
    """The date an email arrived, as YYYY-MM-DD."""
    return (TODAY - timedelta(days=days_ago)).isoformat()


def on(days_ahead: int) -> str:
    """A date in the email's text, e.g. 'October 12, 2026'."""
    d = TODAY + timedelta(days=days_ahead)
    return f"{d:%B} {d.day}, {d.year}"


LAST_MONTH = (TODAY.replace(day=1) - timedelta(days=1))

UNSUB = lambda d: {"List-Unsubscribe": f"<https://{d}/unsubscribe?u=123>, <mailto:unsubscribe@{d}>",  # noqa: E731
                   "List-Unsubscribe-Post": "List-Unsubscribe=One-Click"}

DEMO_EMAILS = [
    Email("d01", "Northwind <news@mail.northwind.example>", "Members only: 20% off your next order",
          "Members-only offer! Take 20% off your next order with code FALL20OFF.\n"
          "Valid until " + on(15) + " on orders over $75. Excludes collaborations and sale items. "
          "One use per customer.\nShop now: https://www.northwind.example/fall\nUnsubscribe anytime.",
          sent(7), UNSUB("northwind.example")),
    Email("d02", "Northwind <news@mail.northwind.example>", "Happy birthday! A gift from Northwind",
          "Happy birthday from all of us at Northwind! Enjoy $10 off with code BDAY10NW.\n"
          "Valid through " + on(35) + ". Minimum purchase $50, online only.\nUnsubscribe anytime.",
          sent(5), UNSUB("northwind.example")),
    Email("d03", "Northwind <news@mail.northwind.example>", "ThermaLayer is back — discover the new collection",
          "New arrivals are here. Discover the new ThermaLayer collection, now warmer and softer than ever.\n"
          "Shop now: https://www.northwind.example/thermalayer\nUnsubscribe anytime.",
          sent(4), UNSUB("northwind.example")),
    Email("d04", "Fernway <hello@email.fernway.example>", "Members: 15% off this week",
          "Members get 15% off: use code FERNFALL15 at checkout.\nValid until " + on(20) + ". "
          "Cannot be combined with other offers.\nUnsubscribe here.", sent(4), UNSUB("fernway.example")),
    Email("d05", "Lumen & Lace <news@lumenlace.example>", "A little something for you",
          "Take 10% off your next purchase with promo code LUMEN10NOW. Valid until " + on(30) + ", "
          "online only.\nUnsubscribe.", sent(3), UNSUB("lumenlace.example")),
    Email("d06", "Harbor Denim <offers@email.harbordenim.example>", "Friends & Family: 30% off",
          "Friends & Family is here: 30% off everything with code HARBOR30. Ends " + on(3) + ". "
          "Exclusions apply.\nUnsubscribe.", sent(2), UNSUB("harbordenim.example")),
    Email("d07", "Bright Smile Dental <reminders@brightsmiledental.example>", "Appointment reminder",
          "Hi! This is a reminder that your appointment with Dr. Patel is scheduled for " + on(6) + " at 3:30 PM.\n"
          "Please reply C to confirm, or call us at (555) 555-0142 to reschedule.", sent(2)),
    Email("d08", "Metro Power & Gas <billing@metropower.example>", "Your bill is ready",
          f"Your {LAST_MONTH:%B} bill is ready. Amount due: $142.37.\nPayment due by {on(8)}.\n"
          "Account number: 4821-7733-0091.\nPay online at https://www.metropower.example/pay", sent(1)),
    Email("d09", "Skyline Mobile <bills@skylinemobile.example>", "Your Skyline Mobile bill is available",
          f"Your bill for {LAST_MONTH:%b %Y} is available. Total due: $85.00. Please pay by {on(14)} to avoid a "
          "late fee.\nAccount #: 99812-00017.", sent(1)),
    Email("d10", "The Daily Scroll <digest@dailyscroll.example>", "This week in tech: issue #214",
          "Top stories this week in tech. Read more about chips, rockets and robots.\n"
          "You're receiving this newsletter because you signed up. Unsubscribe: https://dailyscroll.example/unsub?id=9",
          sent(1), {"List-Unsubscribe": "<https://dailyscroll.example/unsub?id=9>"}),
    Email("d11", "Maya Chen <maya.chen@example.com>", "Dinner Saturday?",
          "Hey! Are you free for dinner Saturday? Thinking that new Thai place around 7. Let me know!",
          sent(0)),
    Email("d12", "Riverton Finance Department <noreply@finance.riverton.example>", "Your property tax exemption was approved",
          "Dear homeowner,\nWe are pleased to inform you that your homestead exemption application has been approved. "
          "The credit will be reflected on your next property tax bill.\nCase number: HX-2026-448120.\n"
          "No further action is needed. Please keep this letter for your records.", sent(0)),
]

# Answers the "user" gives, per email, in order of the questions asked.
DEMO_ANSWERS: dict[str, list[str]] = {
    "d01": ["Save the coupon code and its conditions to my notes, then delete the email.", "1"],
    "d02": [],                                     # learned rule applies automatically
    "d03": ["No coupon? Just archive it.", "1"],     # rule doesn't fully apply -> ask
    "d04": ["1"],                                  # reuse Northwind workflow, make an Fernway rule
    "d05": ["2"],                                  # reuse it, and generalise to any sender
    "d06": [],                                     # general coupon rule applies automatically
    "d07": ["Put it on my to-do list with the appointment time, then archive the email.", "1"],
    "d08": ["Add a todo to pay it by the due date, save the account number and amount to my notes, "
            "and archive the email.", "2"],        # rule for bills from any sender
    "d09": [],                                     # bill rule applies automatically
    "d10": ["Unsubscribe and delete it.", "1"],
    "d11": ["Leave it in my inbox.", "3"],         # one-off, don't learn
    "d12": ["v", "r"],                             # view the original, then: just read & archive, no rule
}
