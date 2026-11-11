# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.1.0] — 2026-11-10

### Added
- **Insights** tab: how much the agent handles on its own, questions per 100 emails (and how that changed), emails you put back after an automatic action, the most-used rules, senders you keep being asked about, what was cleared out, median read time and an estimate of time saved. Practice and demo mail are not counted. Also available at `/api/insights`.
- **Look** tab: Light, Dark or Match my Mac, and an accent colour (lime by default; orange, blue or pink). Saved in the browser.
- The session summary lists the emails skipped in auto mode, and says which sender you were asked about most.
- A move the mail server does not confirm is flagged clearly in the email's card; the email stays in your inbox. The card now has a **Try again now** button (available once the session is stopped or finished) that redoes exactly that move.
- Phone and tablet layouts: the header stacks, buttons stay within the screen and the answer box is tall enough to read its hint.

### Changed
- New design: grey, dashed panels, round buttons and one small bright accent, with the design fonts (Orbitron for titles, Share Tech Mono for text) built into the page, so nothing is loaded from the internet. Licences are in `licenses/`.
- New desktop layout: big centred title, a 3-step start (1 Mailbox, 2 Reader, 3 Start), a "How it works" / "Your setup" panel beside it, and each reader shown with privacy, speed and accuracy bars.
- The page now fits the window: the header stays in place and only the email column and the sidebar scroll. Finished emails fold to one line, dates show "overdue" or a relative time, and the quick answers are shorter.
- New app and browser-tab icon: lime envelope with two white sparkles on a black tile (source: `packaging/mac/icon.svg`).
- The activity log now also records the outcome (auto, asked, one-off, failed), the rule used and the reading time of each email.
- Putting an email back and deleting a rule are recorded in `events.json` in the data folder, so Insights can count them.

## [1.0.0] — 2026-11-08

First public release.

### Features
- Reads new mail over IMAP (Gmail and other providers) and works out what each email is and what's in it.
- Asks in plain English about unfamiliar emails and turns the answer into a reusable rule; applies rules automatically, suggests reusing a rule for similar senders, and asks when a rule only partly fits.
- Actions: save to notes, add a to-do, unsubscribe, archive, delete, keep, or "show me first, then decide".
- Three readers: built-in (offline), local AI through Ollama, or Claude through the Anthropic API.
- Browser app on your own computer, plus a command line (`mail-agent`); a demo inbox with 12 sample emails.
- Auto mode for working through a whole inbox without questions, with live counts.
- To-dos and notes with AI-written titles, the one useful link, date chips, newest-first order, note-to-to-do conversion, and clean-up of old items with undo.
- Importance tags and scam warnings; formatted email preview with scripts removed and remote images blocked.
- Practice mode, a saved copy of every handled email, and restore to inbox.
- Passwords stored in the macOS Keychain.
- Double-click Mac app for Apple Silicon (`Mail Agent.app`), built automatically for each release: runs one copy at a time, has a Quit button, and quits by itself when idle.
