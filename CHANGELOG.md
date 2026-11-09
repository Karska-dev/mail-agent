# Changelog

All notable changes to this project are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

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
